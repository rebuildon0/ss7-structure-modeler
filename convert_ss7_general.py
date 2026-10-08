#!/usr/bin/env python3
"""Convert a Super Build/SS7 all-items CSV export to Rhino-friendly 3D files.

The converter derives stories, levels, axes and member availability from each
CSV instead of assuming a particular building. Native 3DM output is written
when the optional ``rhino3dm`` package is available; DXF and OBJ are always
written.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def clean(value: str) -> str:
    return value.strip().translate(FULLWIDTH_DIGITS).replace("’", "'")


def number(value: str, default: float = 0.0) -> float:
    try:
        return float(clean(value))
    except (TypeError, ValueError):
        return default


def read_sections(csv_path: Path) -> tuple[dict[str, list[list[str]]], list[list[str]], str]:
    text = None
    encoding_used = ""
    for encoding in ("utf-8-sig", "cp932"):
        try:
            text = csv_path.read_text(encoding=encoding)
            encoding_used = encoding
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError(f"CSV encoding is neither UTF-8 nor CP932: {csv_path}")
    rows = list(csv.reader(text.splitlines()))
    sections: dict[str, list[list[str]]] = {}
    i = 0
    while i < len(rows):
        row = rows[i]
        if row and clean(row[0]).startswith("name="):
            name = clean(row[0])[5:]
            j = i + 1
            while j < len(rows) and (not rows[j] or clean(rows[j][0]) != "<data>"):
                if rows[j] and clean(rows[j][0]).startswith("name="):
                    break
                j += 1
            data: list[list[str]] = []
            if j < len(rows) and rows[j] and clean(rows[j][0]) == "<data>":
                j += 1
                while j < len(rows):
                    if not rows[j] or not any(clean(v) for v in rows[j]):
                        break
                    if clean(rows[j][0]).startswith("name="):
                        break
                    values = [clean(v) for v in rows[j]]
                    while values and values[-1] in ("", "<RE>"):
                        values.pop()
                    if values:
                        data.append(values)
                    j += 1
            sections[name] = data
            i = max(i + 1, j)
        else:
            i += 1
    return sections, rows, encoding_used


# Axis names without an X/Y prefix (e.g. A1, B1) are mapped to a direction by make_model.
AXIS_DIRECTIONS: dict[str, str] = {}
AXIS_PATTERN = None


def axes_in(value: str) -> list[str]:
    if AXIS_PATTERN is not None:
        return AXIS_PATTERN.findall(clean(value))
    return re.findall(r"[XY]\d+[A-Za-z]*", clean(value), flags=re.IGNORECASE)


def axis_dir(axis: str) -> str:
    return AXIS_DIRECTIONS.get(axis.upper(), axis[:1].upper())


def assign_axis_directions(axis_names: list[str], sections: dict[str, list[list[str]]]) -> None:
    """Set AXIS_DIRECTIONS/AXIS_PATTERN for axis names that do not start with X/Y."""
    global AXIS_PATTERN
    AXIS_DIRECTIONS.clear()
    AXIS_PATTERN = None
    if all(axis[:1] in ("X", "Y") for axis in axis_names):
        return
    names = sorted(set(axis_names), key=len, reverse=True)
    AXIS_PATTERN = re.compile(
        r"(?<![0-9A-Za-z])(" + "|".join(re.escape(name) for name in names) + r")(?![0-9A-Za-z])",
        flags=re.IGNORECASE,
    )
    parent = {name: name for name in axis_names}

    def root(name):
        while parent[name] != name:
            name = parent[name]
        return name

    for row in sections.get("基準スパン長", []):
        pair = [axis.upper() for axis in axes_in(row[0])]
        if len(pair) == 2 and pair[0] in parent and pair[1] in parent:
            parent[root(pair[0])] = root(pair[1])
    groups: dict[str, list[str]] = {}
    for name in axis_names:
        groups.setdefault(root(name), []).append(name)
    if len(groups) != 2:
        raise ValueError(f"Axis names must form two span chains (X and Y); found {len(groups)}")
    first, second = groups.values()

    span_counts = {}
    for row in sections.get("基本事項", []):
        for value in row:
            match = re.fullmatch(r"([XY])方向スパン数", clean(value))
            if match and row[-1].strip():
                span_counts[match.group(1)] = int(number(row[-1]))
    x_group = None
    if span_counts.get("X") is not None and span_counts.get("X") != span_counts.get("Y"):
        x_group = next((group for group in (first, second) if len(group) - 1 == span_counts["X"]), None)
    if x_group is None:
        # Nodes are written as "X axis - Y axis" (e.g. 柱配置 "A1 - B4").
        node_texts = [row[1] for row in sections.get("柱配置", []) if len(row) > 1]
        node_texts += [row[0] for row in sections.get("軸振れ", []) if row]
        for text in node_texts:
            nodes = [axis.upper() for axis in axes_in(text)]
            if len(nodes) == 2:
                x_group = first if nodes[0] in first else second
                break
    if x_group is None:
        raise ValueError("Could not decide which axis names are X and which are Y")
    for name in axis_names:
        AXIS_DIRECTIONS[name] = "X" if name in x_group else "Y"


def steel_section(value: str):
    """Return display width/depth from common SS7 steel shape strings."""
    shape = clean(value).upper().replace("×", "X").replace("＊", "X")
    if not shape:
        return None
    numbers = [float(v) for v in re.findall(r"\d+(?:\.\d+)?", shape)]
    if shape.startswith(("H-", "HY-", "BH-", "H ")) and len(numbers) >= 2:
        return numbers[1], numbers[0], "steel"
    if (shape.startswith(("□", "BOX", "BCR", "BCP")) or "BOX" in shape) and numbers:
        depth = numbers[0]
        width = numbers[1] if len(numbers) >= 2 else depth
        return width, depth, "steel"
    if shape.startswith(("○", "P-", "PIPE", "STK")) and numbers:
        return numbers[0], numbers[0], "steel"
    return None


def first_steel_section(row: list[str], preferred: tuple[int, ...] = ()):
    indexes = list(preferred) + [i for i in range(len(row)) if i not in preferred]
    for index in indexes:
        if index < len(row):
            parsed = steel_section(row[index])
            if parsed:
                return parsed
    return None


def node_from(value: str) -> tuple[str, str]:
    axes = [axis.upper() for axis in axes_in(value)]
    xs = [axis for axis in axes if axis_dir(axis) == "X"]
    ys = [axis for axis in axes if axis_dir(axis) == "Y"]
    if len(xs) != 1 or len(ys) != 1:
        raise ValueError(f"Node could not be parsed: {value!r}")
    return xs[0], ys[0]


def segment_from(value: str) -> tuple[tuple[str, str], tuple[str, str]]:
    axes = [axis.upper() for axis in axes_in(value)]
    if len(axes) != 3:
        raise ValueError(f"Frame segment could not be parsed: {value!r}")
    frame, start, end = axes
    if axis_dir(frame) == "Y" and axis_dir(start) == "X" and axis_dir(end) == "X":
        return (start, frame), (end, frame)
    if axis_dir(frame) == "X" and axis_dir(start) == "Y" and axis_dir(end) == "Y":
        return (frame, start), (frame, end)
    raise ValueError(f"Unexpected frame/axis order: {value!r}")


def vadd(a, b):
    return tuple(a[i] + b[i] for i in range(3))


def vsub(a, b):
    return tuple(a[i] - b[i] for i in range(3))


def vmul(a, scalar):
    return tuple(a[i] * scalar for i in range(3))


def vdot(a, b):
    return sum(a[i] * b[i] for i in range(3))


def vcross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def vunit(a):
    length = math.sqrt(vdot(a, a))
    if length < 1e-9:
        raise ValueError("Zero-length vector")
    return vmul(a, 1.0 / length)


def perpendicular_basis(p1, p2, kind: str):
    direction = vunit(vsub(p2, p1))
    z_axis = (0.0, 0.0, 1.0)
    x_axis = (1.0, 0.0, 0.0)
    y_axis = (0.0, 1.0, 0.0)
    if kind == "beam":
        vertical = vsub(z_axis, vmul(direction, vdot(z_axis, direction)))
        if vdot(vertical, vertical) < 1e-9:
            vertical = vsub(y_axis, vmul(direction, vdot(y_axis, direction)))
        v = vunit(vertical)
        u = vunit(vcross(v, direction))
    elif kind == "column":
        horizontal = vsub(x_axis, vmul(direction, vdot(x_axis, direction)))
        if vdot(horizontal, horizontal) < 1e-9:
            horizontal = vsub(y_axis, vmul(direction, vdot(y_axis, direction)))
        u = vunit(horizontal)
        v = vunit(vcross(direction, u))
    else:
        reference = z_axis if abs(vdot(direction, z_axis)) < 0.9 else x_axis
        u = vunit(vcross(direction, reference))
        v = vunit(vcross(direction, u))
    return u, v


def prism_between(p1, p2, width: float, depth: float, kind: str):
    u, v = perpendicular_basis(p1, p2, kind)
    hu = vmul(u, width / 2.0)
    hv = vmul(v, depth / 2.0)
    offsets = [vadd(vmul(hu, -1), vmul(hv, -1)), vadd(hu, vmul(hv, -1)), vadd(hu, hv), vadd(vmul(hu, -1), hv)]
    vertices = [vadd(p1, o) for o in offsets] + [vadd(p2, o) for o in offsets]
    faces = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [1, 2, 6, 5], [2, 3, 7, 6], [3, 0, 4, 7]]
    return vertices, faces


def cylinder_between(p1, p2, diameter: float, sides: int = 10):
    u, v = perpendicular_basis(p1, p2, "brace")
    radius = diameter / 2.0
    ring1 = []
    ring2 = []
    for i in range(sides):
        angle = 2.0 * math.pi * i / sides
        offset = vadd(vmul(u, radius * math.cos(angle)), vmul(v, radius * math.sin(angle)))
        ring1.append(vadd(p1, offset))
        ring2.append(vadd(p2, offset))
    vertices = ring1 + ring2 + [p1, p2]
    faces = []
    for i in range(sides):
        j = (i + 1) % sides
        faces.append([i, j, sides + j, sides + i])
        faces.append([2 * sides, j, i])
        faces.append([2 * sides + 1, sides + i, sides + j])
    return vertices, faces


def unique_polygon(points):
    result = []
    seen = set()
    for point in points:
        key = tuple(round(value, 6) for value in point)
        if key not in seen:
            result.append(point)
            seen.add(key)
    return result


def wall_mesh(p1b, p2b, p2t, p1t, thickness: float):
    center = unique_polygon([p1b, p2b, p2t, p1t])
    if len(center) < 3:
        raise ValueError("Degenerate wall polygon")
    if thickness <= 0.0:
        return center, [list(range(len(center)))]
    dx = dy = length = 0.0
    for point, following in zip(center, center[1:] + center[:1]):
        dx = following[0] - point[0]
        dy = following[1] - point[1]
        length = math.hypot(dx, dy)
        if length >= 1e-9:
            break
    if length < 1e-9:
        raise ValueError("Wall has no horizontal direction")
    offset = (-dy * thickness / (2.0 * length), dx * thickness / (2.0 * length), 0.0)
    vertices = [vsub(point, offset) for point in center] + [vadd(point, offset) for point in center]
    count = len(center)
    faces = [list(reversed(range(count))), list(range(count, 2 * count))]
    for index in range(count):
        following = (index + 1) % count
        faces.append([index, following, count + following, count + index])
    return vertices, faces


def combine_meshes(meshes):
    vertices = []
    faces = []
    for mesh_vertices, mesh_faces in meshes:
        offset = len(vertices)
        vertices.extend(mesh_vertices)
        faces.extend([[offset + index for index in face] for face in mesh_faces])
    return vertices, faces


def wall_mesh_with_openings(p1b, p2b, p2t, p1t, thickness: float, openings):
    """Create a wall mesh with rectangular openings in its local elevation.

    Openings use millimetres measured from the wall's external bounding
    rectangle: left/right along the directed wall and bottom/top vertically.
    Each remaining grid cell is a closed panel when the wall has thickness,
    leaving actual empty geometry at every opening.
    """
    if not openings:
        return wall_mesh(p1b, p2b, p2t, p1t, thickness)
    plan_length = math.hypot(p2b[0] - p1b[0], p2b[1] - p1b[1])
    wall_height = max(p1t[2], p2t[2]) - min(p1b[2], p2b[2])
    if plan_length < 1e-9 or wall_height < 1e-9:
        raise ValueError("Wall opening host has no usable elevation rectangle")

    x_cuts = {0.0, plan_length}
    z_cuts = {0.0, wall_height}
    for opening in openings:
        x_cuts.update((opening["left_mm"], opening["right_mm"]))
        z_cuts.update((opening["bottom_mm"], opening["top_mm"]))
    x_values = sorted(x_cuts)
    z_values = sorted(z_cuts)

    def surface_point(x_value, z_value):
        s = x_value / plan_length
        t = z_value / wall_height
        bottom = vadd(p1b, vmul(vsub(p2b, p1b), s))
        top = vadd(p1t, vmul(vsub(p2t, p1t), s))
        return vadd(bottom, vmul(vsub(top, bottom), t))

    panel_meshes = []
    for x1, x2 in zip(x_values, x_values[1:]):
        for z1, z2 in zip(z_values, z_values[1:]):
            if x2 - x1 < 1e-9 or z2 - z1 < 1e-9:
                continue
            center_x = (x1 + x2) / 2.0
            center_z = (z1 + z2) / 2.0
            if any(
                opening["left_mm"] < center_x < opening["right_mm"]
                and opening["bottom_mm"] < center_z < opening["top_mm"]
                for opening in openings
            ):
                continue
            panel_meshes.append(wall_mesh(
                surface_point(x1, z1), surface_point(x2, z1),
                surface_point(x2, z2), surface_point(x1, z2), thickness,
            ))
    if not panel_meshes:
        raise ValueError("Wall openings removed the complete host wall")
    return combine_meshes(panel_meshes)


def plan_coordinate(point, axis):
    return point[0] * axis[0] + point[1] * axis[1]


def clip_polygon_at_coordinate(polygon, axis, cut, keep_lower):
    """Clip a 3D polygon by a vertical plan half-plane."""
    result = []
    tolerance = 1e-7
    for start, end in zip(polygon, polygon[1:] + polygon[:1]):
        start_value = plan_coordinate(start, axis) - cut
        end_value = plan_coordinate(end, axis) - cut
        start_inside = start_value <= tolerance if keep_lower else start_value >= -tolerance
        end_inside = end_value <= tolerance if keep_lower else end_value >= -tolerance
        if start_inside:
            result.append(start)
        if start_inside != end_inside:
            denominator = start_value - end_value
            if abs(denominator) > tolerance:
                ratio = start_value / denominator
                result.append(vadd(start, vmul(vsub(end, start), ratio)))
    return unique_polygon(result)


def split_polygon_at_coordinate(polygon, beam_axis, split_axis, cut):
    lower = clip_polygon_at_coordinate(polygon, split_axis, cut, True)
    upper = clip_polygon_at_coordinate(polygon, split_axis, cut, False)
    intersections = []
    tolerance = 1e-7
    for start, end in zip(polygon, polygon[1:] + polygon[:1]):
        start_value = plan_coordinate(start, split_axis) - cut
        end_value = plan_coordinate(end, split_axis) - cut
        if abs(start_value) <= tolerance:
            intersections.append(start)
        if start_value * end_value < -(tolerance ** 2):
            ratio = start_value / (start_value - end_value)
            intersections.append(vadd(start, vmul(vsub(end, start), ratio)))
    intersections = unique_polygon(intersections)
    if len(lower) < 3 or len(upper) < 3 or len(intersections) < 2:
        raise ValueError("Floor-framing split does not cross the floor polygon")
    intersections.sort(key=lambda point: plan_coordinate(point, beam_axis))
    return lower, upper, intersections[0], intersections[-1]


def floor_gap_lengths(total: float, count: int, raw_values):
    """Resolve SS7 floor-framing spans into count+1 positive gaps."""
    if count <= 0 or total <= 0.0:
        return []
    values = [number(raw_values[index]) if index < len(raw_values) else 0.0 for index in range(count + 1)]
    if all(abs(value) < 1e-9 for value in values):
        return [total / (count + 1)] * (count + 1)
    fixed = sum(value for value in values if value > 0.0)
    flexible_indexes = [index for index, value in enumerate(values) if value <= 0.0]
    if flexible_indexes and fixed < total - 1e-9:
        remaining = total - fixed
        weights = [abs(values[index]) if values[index] < 0.0 else 1.0 for index in flexible_indexes]
        weight_total = sum(weights)
        result = list(values)
        for index, weight in zip(flexible_indexes, weights):
            result[index] = remaining * weight / weight_total
        return result
    positive = [max(value, 0.0) for value in values]
    positive_total = sum(positive)
    if positive_total <= 1e-9:
        return [total / (count + 1)] * (count + 1)
    return [value * total / positive_total for value in positive]


def slab_mesh(corners, thickness: float):
    corners = unique_polygon(corners)
    if len(corners) < 3:
        raise ValueError("Degenerate slab polygon")
    area2 = 0.0
    for point, following in zip(corners, corners[1:] + corners[:1]):
        area2 += point[0] * following[1] - following[0] * point[1]
    if abs(area2) < 1e-6:
        raise ValueError("Degenerate slab area")
    if thickness <= 0.0:
        return corners, [list(range(len(corners)))]
    bottom = [(x, y, z - thickness) for x, y, z in corners]
    vertices = bottom + corners
    count = len(corners)
    faces = [list(reversed(range(count))), list(range(count, 2 * count))]
    for index in range(count):
        following = (index + 1) % count
        faces.append([index, following, count + following, count + index])
    return vertices, faces


def tapered_cantilever_slab_mesh(corners, root_thickness: float, tip_thickness: float):
    """Create a four-sided cantilever slab with root and tip thicknesses."""
    corners = unique_polygon(corners)
    if len(corners) != 4:
        raise ValueError("Cantilever slab must have four unique corners")
    area2 = 0.0
    for point, following in zip(corners, corners[1:] + corners[:1]):
        area2 += point[0] * following[1] - following[0] * point[1]
    if abs(area2) < 1e-6:
        raise ValueError("Degenerate cantilever slab area")
    root_thickness = max(root_thickness, 0.0)
    tip_thickness = max(tip_thickness, 0.0)
    if root_thickness <= 0.0 and tip_thickness <= 0.0:
        return corners, [list(range(4))]
    thicknesses = [root_thickness, root_thickness, tip_thickness, tip_thickness]
    bottom = [(x, y, z - thicknesses[index]) for index, (x, y, z) in enumerate(corners)]
    vertices = bottom + corners
    faces = [[3, 2, 1, 0], [4, 5, 6, 7]]
    for index in range(4):
        following = (index + 1) % 4
        faces.append([index, following, 4 + following, 4 + index])
    return vertices, faces


def make_model(csv_path: Path) -> dict:
    sections, all_rows, source_encoding = read_sections(csv_path)
    required = ["軸名", "基準スパン長", "階名", "層名", "標準階高"]
    missing = [name for name in required if not sections.get(name)]
    if missing:
        raise ValueError("Required SS7 sections are missing or empty: " + ", ".join(missing))

    axis_names = [clean(row[0]).upper() for row in sections["軸名"] if row]
    assign_axis_directions(axis_names, sections)
    x_axes = [axis for axis in axis_names if axis_dir(axis) == "X"]
    y_axes = [axis for axis in axis_names if axis_dir(axis) == "Y"]
    if not x_axes or not y_axes:
        raise ValueError("Both X and Y axis names are required")

    span_rows = []
    for row in sections["基準スパン長"]:
        pair = [axis.upper() for axis in axes_in(row[0])]
        if len(pair) == 2 and len(row) > 1:
            span_rows.append((pair[0], pair[1], number(row[1])))

    def build_axis_positions(names, prefix):
        positions = {names[0]: 0.0}
        relevant = [(a, b, length) for a, b, length in span_rows if axis_dir(a) == prefix and axis_dir(b) == prefix]
        for _ in range(len(names) + len(relevant)):
            changed = False
            for start, end, length in relevant:
                if start in positions and end not in positions:
                    positions[end] = positions[start] + length
                    changed = True
                elif end in positions and start not in positions:
                    positions[start] = positions[end] - length
                    changed = True
            if not changed:
                break
        unresolved = [axis for axis in names if axis not in positions]
        if unresolved:
            raise ValueError(f"Could not resolve {prefix}-axis positions: {unresolved}")
        return {axis: positions[axis] for axis in names}

    x_pos = build_axis_positions(x_axes, "X")
    y_pos = build_axis_positions(y_axes, "Y")

    stories_top_down = [clean(row[0]) for row in sections["階名"] if row]
    layers_top_down = [clean(row[0]) for row in sections["層名"] if row]
    if len(layers_top_down) != len(stories_top_down) + 1:
        raise ValueError(
            f"Layer/story mismatch: {len(layers_top_down)} layers for {len(stories_top_down)} stories"
        )
    story_heights = {clean(row[0]): number(row[1]) for row in sections["標準階高"] if len(row) > 1}
    missing_heights = [story for story in stories_top_down if story_heights.get(story, 0.0) <= 0.0]
    if missing_heights:
        raise ValueError("Positive standard story heights are missing: " + ", ".join(missing_heights))
    story_to_layers = {
        story: (layers_top_down[index + 1], layers_top_down[index])
        for index, story in enumerate(stories_top_down)
    }
    story_order = list(reversed(stories_top_down))
    layer_z = {layers_top_down[-1]: 0.0}
    for story in story_order:
        bottom, top = story_to_layers[story]
        layer_z[top] = layer_z[bottom] + story_heights[story]

    shifts: dict[tuple[str, str], tuple[float, float]] = {}
    for row in sections.get("軸振れ", []):
        if len(row) >= 3:
            shifts[node_from(row[0])] = (number(row[1]), number(row[2]))

    z_moves: dict[tuple[str, str, str], float] = {}
    for row in sections.get("節点の上下移動", []):
        if len(row) >= 3:
            layer = clean(row[0])
            x_axis, y_axis = node_from(row[1])
            z_moves[(layer, x_axis, y_axis)] = number(row[2])

    same_nodes: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    for row in sections.get("節点の同一化", []):
        if len(row) >= 4:
            src_x, src_y = node_from(row[1])
            dst_x, dst_y = node_from(row[3])
            same_nodes[(clean(row[0]), src_x, src_y)] = (clean(row[2]), dst_x, dst_y)

    def resolve_node(layer: str, node: tuple[str, str]):
        key = (clean(layer), node[0], node[1])
        seen = set()
        while key in same_nodes:
            if key in seen:
                raise ValueError(f"Cyclic same-node mapping at {key}")
            seen.add(key)
            key = same_nodes[key]
        return key

    def node_point(layer: str, node: tuple[str, str]):
        resolved_layer, x_axis, y_axis = resolve_node(layer, node)
        if resolved_layer not in layer_z:
            raise ValueError(f"Unknown layer after same-node mapping: {resolved_layer}")
        if x_axis not in x_pos or y_axis not in y_pos:
            raise ValueError(f"Unknown axis after same-node mapping: {(x_axis, y_axis)}")
        dx, dy = shifts.get((x_axis, y_axis), (0.0, 0.0))
        dz = z_moves.get((resolved_layer, x_axis, y_axis), 0.0)
        return (x_pos[x_axis] + dx, y_pos[y_axis] + dy, layer_z[resolved_layer] + dz)

    column_sections = {}
    for row in sections.get("木質柱断面", []):
        if len(row) > 4:
            column_sections[(clean(row[0]), clean(row[1]))] = (number(row[3]), number(row[4]), "wood")
    for row in sections.get("RC柱断面", []):
        if len(row) > 5:
            column_sections[(clean(row[0]), clean(row[1]))] = (number(row[4]), number(row[5]), "rc")
    for section_name in ("S柱断面", "CFT柱断面"):
        for row in sections.get(section_name, []):
            if len(row) > 1:
                parsed = first_steel_section(row, (5, 7))
                if parsed:
                    column_sections[(clean(row[0]), clean(row[1]))] = parsed

    beam_sections = {}
    for row in sections.get("木質梁断面", []):
        if len(row) > 4:
            beam_sections[(clean(row[0]), clean(row[1]))] = (number(row[3]), number(row[4]), "wood")
    for row in sections.get("RC梁断面", []):
        if len(row) > 8:
            beam_sections[(clean(row[0]), clean(row[1]))] = (number(row[7]), number(row[8]), "rc")
    for row in sections.get("S梁断面", []):
        if len(row) > 1:
            parsed = first_steel_section(row, (9, 7, 11))
            if parsed:
                beam_sections[(clean(row[0]), clean(row[1]))] = parsed

    small_beam_sections = {}
    for row in sections.get("RC小梁断面", []):
        if len(row) > 2:
            small_beam_sections[clean(row[0])] = (number(row[1]), number(row[2]), "rc")
    for row in sections.get("木質小梁断面", []):
        if len(row) > 2:
            small_beam_sections[clean(row[0])] = (number(row[1]), number(row[2]), "wood")
    for row in sections.get("S小梁断面", []):
        if len(row) > 1:
            parsed = first_steel_section(row, (3,))
            if parsed:
                small_beam_sections[clean(row[0])] = parsed

    brace_sections = {}
    for row in sections.get("木質ブレース断面", []):
        if len(row) > 2:
            brace_sections[clean(row[0])] = (number(row[1]), number(row[2]), "wood")
    for section_name in ("Sブレース断面", "鉛直ブレース断面"):
        for row in sections.get(section_name, []):
            if len(row) > 1:
                parsed = first_steel_section(row)
                if parsed:
                    brace_sections[clean(row[0])] = parsed

    wall_sections = {}
    for row in sections.get("壁断面", []):
        if len(row) > 1:
            wall_sections[clean(row[0])] = number(row[1])
    slab_sections = {}
    for row in sections.get("床断面", []):
        if len(row) > 1:
            slab_sections[clean(row[0])] = number(row[1])
    offframe_wall_sections = {}
    for row in sections.get("フレーム外雑壁断面", []):
        if len(row) > 1:
            offframe_wall_sections[clean(row[0])] = number(row[1])
    cantilever_slab_sections = {}
    for row in sections.get("片持床断面", []):
        if len(row) > 1:
            nominal = number(row[1])
            root = number(row[2], nominal) if len(row) > 2 else nominal
            tip = number(row[3], nominal) if len(row) > 3 else nominal
            cantilever_slab_sections[clean(row[0])] = (nominal, root, tip)

    hbrace_diameters = {}
    for row in sections.get("水平ブレース断面（引張ブレース）", []):
        if len(row) > 1:
            match = re.search(r"TB-M(\d+(?:\.\d+)?)", row[1], flags=re.IGNORECASE)
            hbrace_diameters[clean(row[0])] = float(match.group(1)) if match else 20.0

    objects = []
    centerlines = []
    fallback_sections = Counter()
    warning_counts = Counter()

    def beam_level_control(value: str) -> str:
        control = clean(value).replace("押え", "").replace("押さえ", "")
        aliases = {
            "上": "上面", "上端": "上面", "上面": "上面",
            "中": "中心", "中央": "中心", "中心": "中心",
            "下": "下面", "下端": "下面", "下面": "下面",
        }
        return aliases.get(control, control)

    def beam_center_offset(depth: float, control: str, dimension: float) -> float:
        """Return the beam center Z offset from the SS7 standard layer in mm.

        SS7 layer heights use the beam top as their standard reference.  A
        positive level-adjustment dimension is upward from that reference.
        """
        if control == "上面":
            return dimension - depth / 2.0
        if control == "中心":
            return dimension
        if control == "下面":
            return dimension + depth / 2.0
        raise ValueError(f"Unknown beam level control: {control!r}")

    beam_level_defaults = {}
    for row in sections.get("梁のレベル調整", []):
        if len(row) < 3 or clean(row[0]) not in layer_z:
            warning_counts["malformed layer beam-level rows skipped"] += 1
            continue
        control = beam_level_control(row[1])
        if control not in ("上面", "中心", "下面"):
            warning_counts["layer beam-level rows with unknown controls skipped"] += 1
            continue
        beam_level_defaults[clean(row[0])] = {
            "control": control,
            "dimension_mm": number(row[2]),
        }

    beam_level_overrides = {}
    beam_level_override_rows = len(sections.get("大梁のレベル調整", []))
    for row in sections.get("大梁のレベル調整", []):
        if len(row) < 4 or clean(row[0]) not in layer_z:
            warning_counts["malformed individual girder-level rows skipped"] += 1
            continue
        try:
            node1, node2 = segment_from(row[1])
        except ValueError:
            warning_counts["unparsed individual girder-level rows skipped"] += 1
            continue
        control = beam_level_control(row[2])
        if control not in ("上面", "中心", "下面"):
            warning_counts["individual girder-level rows with unknown controls skipped"] += 1
            continue
        key = (clean(row[0]), tuple(sorted((node1, node2))))
        if key in beam_level_overrides:
            warning_counts["duplicate individual girder-level rows overwritten"] += 1
        beam_level_overrides[key] = {
            "control": control,
            "dimension_mm": number(row[3]),
            "source": clean(row[1]),
        }

    matched_beam_level_keys = set()
    beam_level_source_counts = Counter()
    beam_level_control_counts = Counter()

    beam_specs = []
    for row in sections.get("大梁配置", []):
        if len(row) < 3:
            warning_counts["malformed girder rows skipped"] += 1
            continue
        layer, symbol = clean(row[0]), clean(row[2])
        if layer not in layer_z:
            warning_counts["girder rows with unknown layers skipped"] += 1
            continue
        try:
            node1, node2 = segment_from(row[1])
        except ValueError:
            warning_counts["unparsed girder rows skipped"] += 1
            continue
        beam_key = (layer, tuple(sorted((node1, node2))))
        section = beam_sections.get((layer, symbol))
        section_is_fallback = not section or section[0] <= 0 or section[1] <= 0
        if section_is_fallback:
            section = (100.0, 100.0, "fallback")
        level_setting = beam_level_overrides.get(beam_key)
        if level_setting:
            level_source = "individual"
        elif layer in beam_level_defaults:
            level_setting = beam_level_defaults[layer]
            level_source = "layer"
        else:
            level_setting = {"control": "上面", "dimension_mm": 0.0}
            level_source = "implicit-default"
        center_offset = beam_center_offset(section[1], level_setting["control"], level_setting["dimension_mm"])
        base_p1 = node_point(layer, node1)
        base_p2 = node_point(layer, node2)
        p1 = (base_p1[0], base_p1[1], base_p1[2] + center_offset)
        p2 = (base_p2[0], base_p2[1], base_p2[2] + center_offset)
        beam_specs.append({
            "row": row, "layer": layer, "symbol": symbol,
            "node1": node1, "node2": node2, "beam_key": beam_key,
            "section": section, "section_is_fallback": section_is_fallback,
            "level_setting": level_setting, "level_source": level_source,
            "center_offset": center_offset, "base_p1": base_p1, "base_p2": base_p2,
            "p1": p1, "p2": p2,
        })

    incident_beam_faces = defaultdict(list)
    for spec in beam_specs:
        if math.dist(spec["p1"], spec["p2"]) < 1e-6:
            continue
        depth = spec["section"][1]
        for node, base_point in ((spec["node1"], spec["base_p1"]), (spec["node2"], spec["base_p2"])):
            center_z = base_point[2] + spec["center_offset"]
            incident_beam_faces[resolve_node(spec["layer"], node)].append({
                "top_z": center_z + depth / 2.0,
                "bottom_z": center_z - depth / 2.0,
                "source": clean(spec["row"][1]),
                "symbol": spec["symbol"],
            })

    # Members other than girders follow the girder levels.  Offsets are the
    # girder top relative to its node on the SS7 layer (beam_center_offset + depth/2).
    lowest_layer = layers_top_down[-1]
    level_follow_counts = Counter()
    girder_top_offset = {}
    girders_by_layer = defaultdict(list)
    node_top_offsets = defaultdict(list)
    for spec in beam_specs:
        offset = spec["center_offset"] + spec["section"][1] / 2.0
        girder_top_offset[spec["beam_key"]] = offset
        girders_by_layer[spec["layer"]].append((spec["node1"], spec["node2"], offset))
        for node in (spec["node1"], spec["node2"]):
            node_top_offsets[resolve_node(spec["layer"], node)].append(offset)

    def layer_default_top_offset(layer):
        setting = beam_level_defaults.get(layer)
        if not setting:
            return 0.0
        if setting["control"] == "上面":
            return setting["dimension_mm"]
        return None

    def fallback_offset(kind, layer):
        default = layer_default_top_offset(layer)
        if default is None:
            level_follow_counts[f"{kind}: unchanged"] += 1
            return 0.0, "unchanged"
        level_follow_counts[f"{kind}: layer-default"] += 1
        return default, "layer-default"

    def segment_top_offset(kind, layer, node1, node2):
        offset = girder_top_offset.get((layer, tuple(sorted((node1, node2)))))
        if offset is not None:
            level_follow_counts[f"{kind}: girder"] += 1
            return offset, "girder"
        return fallback_offset(kind, layer)

    def node_top_offset(kind, layer, node, lowest=True):
        offsets = node_top_offsets.get(resolve_node(layer, node))
        if offsets:
            level_follow_counts[f"{kind}: girder"] += 1
            return (min(offsets) if lowest else max(offsets)), "girder"
        return fallback_offset(kind, layer)

    def panel_top_offset(kind, layer, xs, ys):
        """Lowest girder top on the boundary of a slab panel."""
        x_lo, x_hi = sorted(x_pos[axis] for axis in xs)
        y_lo, y_hi = sorted(y_pos[axis] for axis in ys)
        offsets = []
        for (ax1, ay1), (ax2, ay2), offset in girders_by_layer.get(layer, []):
            if ay1 == ay2 and ay1 in ys:
                lo, hi = sorted((x_pos[ax1], x_pos[ax2]))
                if min(hi, x_hi) - max(lo, x_lo) > 1e-6:
                    offsets.append(offset)
            elif ax1 == ax2 and ax1 in xs:
                lo, hi = sorted((y_pos[ay1], y_pos[ay2]))
                if min(hi, y_hi) - max(lo, y_lo) > 1e-6:
                    offsets.append(offset)
        if offsets:
            level_follow_counts[f"{kind}: girder"] += 1
            return min(offsets), "girder"
        return fallback_offset(kind, layer)

    def lift(point, delta):
        return (point[0], point[1], point[2] + delta)

    column_length_counts = Counter()
    column_extension_total_mm = 0.0
    column_max_endpoint_extension_mm = 0.0

    def add_object(kind, level, symbol, vertices, faces, source, section_mm, placement_adjustments=None):
        sequence = 1 + sum(1 for obj in objects if obj["kind"] == kind)
        obj = {
            "name": f"{kind}_{level}_{symbol}_{sequence:03d}",
            "layer": f"{kind}_{level}".replace("'", "_"),
            "kind": kind,
            "level": level,
            "symbol": symbol,
            "source": source,
            "section_mm": list(section_mm),
            "vertices": [[round(v, 6) for v in point] for point in vertices],
            "faces": faces,
        }
        if placement_adjustments:
            obj["placement_adjustments"] = placement_adjustments
        objects.append(obj)

    def add_linear(kind, level, symbol, p1, p2, width, depth, shape="prism", source="", placement_adjustments=None):
        if math.dist(p1, p2) < 1e-6:
            warning_counts[f"zero-length {kind} rows skipped"] += 1
            return False
        if shape == "cylinder":
            vertices, faces = cylinder_between(p1, p2, width)
        else:
            basis_kind = "column" if kind == "COLUMNS" else "beam" if kind in ("BEAMS", "SMALL_BEAMS") else "brace"
            vertices, faces = prism_between(p1, p2, width, depth, basis_kind)
        add_object(kind, level, symbol, vertices, faces, source, (width, depth), placement_adjustments)
        centerline = {"kind": kind, "level": level, "symbol": symbol, "p1": list(p1), "p2": list(p2)}
        if placement_adjustments:
            centerline["placement_adjustments"] = placement_adjustments
        centerlines.append(centerline)
        return True

    for row in sections.get("柱配置", []):
        if len(row) < 3:
            warning_counts["malformed column rows skipped"] += 1
            continue
        story, symbol = clean(row[0]), clean(row[2])
        if story not in story_to_layers:
            warning_counts["column rows with unknown stories skipped"] += 1
            continue
        node = node_from(row[1])
        bottom, top = story_to_layers[story]
        section = column_sections.get((story, symbol))
        if not section or section[0] <= 0 or section[1] <= 0:
            section = (100.0, 100.0, "fallback")
            fallback_sections[f"column {story}/{symbol}"] += 1
        bottom_point = node_point(bottom, node)
        top_point = node_point(top, node)
        bottom_original_z = bottom_point[2]
        top_original_z = top_point[2]
        bottom_faces = incident_beam_faces.get(resolve_node(bottom, node), [])
        top_faces = incident_beam_faces.get(resolve_node(top, node), [])
        if top_faces:
            top_target_z = max(face["top_z"] for face in top_faces)
            level_follow_counts["COLUMNS top: girder"] += 1
        else:
            top_target_z = top_original_z + fallback_offset("COLUMNS top", top)[0]
        if bottom_faces and bottom == lowest_layer:
            bottom_target_z = min(face["bottom_z"] for face in bottom_faces)
            level_follow_counts["COLUMNS bottom: foundation girder bottom"] += 1
        elif bottom_faces:
            bottom_target_z = max(face["top_z"] for face in bottom_faces)
            level_follow_counts["COLUMNS bottom: girder"] += 1
        elif bottom == lowest_layer:
            bottom_target_z = bottom_original_z
            level_follow_counts["COLUMNS bottom: unchanged"] += 1
        else:
            bottom_target_z = bottom_original_z + fallback_offset("COLUMNS bottom", bottom)[0]
        bottom_adjusted_z = bottom_target_z
        top_adjusted_z = top_target_z
        bottom_extension = bottom_original_z - bottom_adjusted_z
        top_extension = top_adjusted_z - top_original_z
        bottom_point = (bottom_point[0], bottom_point[1], bottom_adjusted_z)
        top_point = (top_point[0], top_point[1], top_adjusted_z)
        column_adjustment = {
            "bottom_layer": bottom,
            "top_layer": top,
            "bottom_original_z_mm": bottom_original_z,
            "bottom_adjusted_z_mm": bottom_adjusted_z,
            "bottom_extension_mm": bottom_extension,
            "bottom_incident_beams": sorted({face["source"] for face in bottom_faces}),
            "top_original_z_mm": top_original_z,
            "top_adjusted_z_mm": top_adjusted_z,
            "top_extension_mm": top_extension,
            "top_incident_beams": sorted({face["source"] for face in top_faces}),
        }
        placement_adjustments = None
        if abs(bottom_extension) > 1e-6 or abs(top_extension) > 1e-6:
            placement_adjustments = {"column_length": column_adjustment}
        column_added = add_linear(
            "COLUMNS", story, symbol, bottom_point, top_point, section[0], section[1],
            source=row[1], placement_adjustments=placement_adjustments,
        )
        if column_added:
            column_length_counts["columns"] += 1
            column_length_counts["endpoints_with_incident_beams"] += int(bool(bottom_faces)) + int(bool(top_faces))
            if bottom_extension > 1e-6:
                column_length_counts["bottom_endpoints_extended"] += 1
            elif bottom_extension < -1e-6:
                column_length_counts["bottom_endpoints_shortened"] += 1
            if top_extension > 1e-6:
                column_length_counts["top_endpoints_extended"] += 1
            elif top_extension < -1e-6:
                column_length_counts["top_endpoints_shortened"] += 1
            if abs(bottom_extension) > 1e-6 or abs(top_extension) > 1e-6:
                column_length_counts["columns_extended"] += 1
                column_extension_total_mm += bottom_extension + top_extension
                column_max_endpoint_extension_mm = max(
                    column_max_endpoint_extension_mm, abs(bottom_extension), abs(top_extension)
                )

    beam_keys = set()
    for spec in beam_specs:
        row = spec["row"]
        layer, symbol = spec["layer"], spec["symbol"]
        beam_key = spec["beam_key"]
        beam_keys.add(beam_key)
        section = spec["section"]
        if spec["section_is_fallback"]:
            fallback_sections[f"girder {layer}/{symbol}"] += 1
        level_setting = spec["level_setting"]
        level_source = spec["level_source"]
        if level_source == "individual":
            matched_beam_level_keys.add(beam_key)
        adjustments = {
            "beam_level_control": level_setting["control"],
            "beam_level_dimension_mm": level_setting["dimension_mm"],
            "beam_center_offset_z_mm": spec["center_offset"],
            "beam_level_source": level_source,
        }
        beam_added = add_linear(
            "BEAMS", layer, symbol, spec["p1"], spec["p2"], section[0], section[1], source=row[1],
            placement_adjustments=adjustments,
        )
        if beam_added:
            beam_level_source_counts[level_source] += 1
            beam_level_control_counts[level_setting["control"]] += 1

    unmatched_beam_level_keys = set(beam_level_overrides) - matched_beam_level_keys
    if unmatched_beam_level_keys:
        warning_counts["individual girder-level rows without matching girders"] = len(unmatched_beam_level_keys)

    floor_shapes = {
        clean(row[0]): row for row in sections.get("床組形状", []) if len(row) >= 35
    }
    small_beam_rows = sections.get("小梁配置", [])
    small_beam_counts = Counter({
        "input_rows": len(small_beam_rows),
        "floor_assembly_input_rows": len(sections.get("床組配置", [])),
        "floor_shape_rows": len(floor_shapes),
    })
    small_beam_placements = defaultdict(lambda: defaultdict(list))
    malformed_small_beam_indexes = set()
    for placement_index, row in enumerate(small_beam_rows):
        if len(row) < 10:
            malformed_small_beam_indexes.add(placement_index)
            warning_counts["malformed small-beam rows skipped"] += 1
            continue
        path = tuple(int(number(row[index])) for index in range(3, min(8, len(row))) if int(number(row[index])) > 0)
        if not path:
            malformed_small_beam_indexes.add(placement_index)
            warning_counts["small-beam rows without hierarchy paths skipped"] += 1
            continue
        key = (clean(row[0]), clean(row[1]), clean(row[2]))
        small_beam_placements[key][path].append((placement_index, row))

    matched_small_beam_indexes = set()
    nondefault_floor_transforms = 0

    def expand_floor_shape(layer, area, floor_key, shape_id, polygon, path_prefix, floor_angle, ancestry):
        shape = floor_shapes.get(shape_id)
        if not shape:
            warning_counts["floor assemblies referencing unknown shapes skipped"] += 1
            return
        if shape_id in ancestry or len(path_prefix) >= 5:
            warning_counts["cyclic or over-deep floor-framing hierarchies skipped"] += 1
            return
        direction = clean(shape[1])
        if direction not in ("X方向", "Y方向"):
            warning_counts["floor shapes with unknown beam directions skipped"] += 1
            return
        beam_count = int(number(shape[2] if direction == "X方向" else shape[3]))
        if beam_count <= 0:
            return
        angle = floor_angle + number(shape[34]) + (0.0 if direction == "X方向" else 90.0)
        radians = math.radians(angle)
        beam_axis = (math.cos(radians), math.sin(radians))
        split_axis = (
            (-math.sin(radians), math.cos(radians)))
        if direction == "Y方向":
            split_axis = (math.sin(radians), -math.cos(radians))
        split_values = [plan_coordinate(point, split_axis) for point in polygon]
        minimum, maximum = min(split_values), max(split_values)
        total = maximum - minimum
        span_values = shape[24:34] if direction == "X方向" else shape[14:24]
        gaps = floor_gap_lengths(total, beam_count, span_values)
        cuts = [minimum + sum(gaps[:index + 1]) for index in range(beam_count)]

        regions = []
        remaining = polygon
        beam_lines = []
        try:
            for cut in cuts:
                lower, upper, line_start, line_end = split_polygon_at_coordinate(
                    remaining, beam_axis, split_axis, cut
                )
                regions.append(lower)
                beam_lines.append((line_start, line_end))
                remaining = upper
            regions.append(remaining)
        except ValueError:
            warning_counts["floor-framing beams outside target floor polygons skipped"] += 1
            return

        placements = small_beam_placements.get(floor_key, {})
        for beam_index, (top_start, top_end) in enumerate(beam_lines, start=1):
            path = path_prefix + (beam_index,)
            candidates = placements.get(path, [])
            if not candidates:
                continue
            placement_index, placement = candidates.pop(0)
            symbol = clean(placement[9])
            section = small_beam_sections.get(symbol)
            if not section or section[0] <= 0.0 or section[1] <= 0.0:
                section = (100.0, 100.0, "fallback")
                fallback_sections[f"small beam {layer}/{symbol}"] += 1
            center_drop = section[1] / 2.0
            p1 = (top_start[0], top_start[1], top_start[2] - center_drop)
            p2 = (top_end[0], top_end[1], top_end[2] - center_drop)
            adjustments = {
                "floor_framing": {
                    "area": area,
                    "floor_shape": shape_id,
                    "hierarchy_path": list(path),
                    "direction": direction,
                    "angle_degrees": angle,
                    "top_reference_z_mm": [top_start[2], top_end[2]],
                }
            }
            if add_linear(
                "SMALL_BEAMS", layer, symbol, p1, p2, section[0], section[1],
                source=f"{area} / {'-'.join(str(value) for value in path)}",
                placement_adjustments=adjustments,
            ):
                matched_small_beam_indexes.add(placement_index)
                small_beam_counts["modeled"] += 1

        child_shapes = [clean(value) for value in shape[4:14]]
        next_ancestry = ancestry | {shape_id}
        for region_index, region in enumerate(regions, start=1):
            child_shape = child_shapes[region_index - 1] if region_index <= len(child_shapes) else "0"
            if child_shape not in ("", "0"):
                expand_floor_shape(
                    layer, area, floor_key, child_shape, region,
                    path_prefix + (region_index,), floor_angle, next_ancestry,
                )

    for row in sections.get("床組配置", []):
        if len(row) < 4:
            warning_counts["malformed floor-assembly rows skipped"] += 1
            continue
        layer, area, double, shape_id = clean(row[0]), clean(row[1]), clean(row[2]), clean(row[3])
        floor_key = (layer, area, double)
        if shape_id in ("", "0") or floor_key not in small_beam_placements:
            continue
        if layer not in layer_z:
            warning_counts["floor assemblies with unknown layers skipped"] += 1
            continue
        axes = [axis.upper() for axis in axes_in(area)]
        xs = [axis for axis in axes if axis_dir(axis) == "X"]
        ys = [axis for axis in axes if axis_dir(axis) == "Y"]
        if len(xs) != 2 or len(ys) != 2:
            warning_counts["unparsed floor-assembly areas skipped"] += 1
            continue
        if len(row) >= 7 and (clean(row[5]) not in ("", "NO") or clean(row[6]) not in ("", "NO")):
            nondefault_floor_transforms += 1
        panel_delta, _ = panel_top_offset("SMALL_BEAMS panels", layer, xs, ys)
        polygon = [lift(node_point(layer, node), panel_delta) for node in (
            (xs[0], ys[0]), (xs[1], ys[0]), (xs[1], ys[1]), (xs[0], ys[1]),
        )]
        small_beam_counts["floor_assemblies_expanded"] += 1
        expand_floor_shape(
            layer, area, floor_key, shape_id, polygon, (),
            number(row[7]) if len(row) > 7 else 0.0, set(),
        )

    unmatched_small_beam_indexes = set(range(len(small_beam_rows))) - matched_small_beam_indexes - malformed_small_beam_indexes
    small_beam_counts["unmatched_rows"] = len(unmatched_small_beam_indexes)
    if unmatched_small_beam_indexes:
        warning_counts["small-beam rows not matched by floor-framing hierarchy"] = len(unmatched_small_beam_indexes)
    if nondefault_floor_transforms:
        warning_counts["floor-assembly X/Y reversals not applied"] = nondefault_floor_transforms

    wall_opening_rows = sections.get("壁開口配置", [])
    wall_opening_counts = Counter({"input_rows": len(wall_opening_rows)})
    wall_openings_by_key = defaultdict(list)
    malformed_opening_indexes = set()
    for opening_index, row in enumerate(wall_opening_rows):
        if len(row) < 8:
            malformed_opening_indexes.add(opening_index)
            warning_counts["malformed wall-opening rows skipped"] += 1
            continue
        try:
            opening_node1, opening_node2 = segment_from(row[1])
        except ValueError:
            malformed_opening_indexes.add(opening_index)
            warning_counts["unparsed wall-opening host segments skipped"] += 1
            continue
        key = (clean(row[0]), tuple(sorted((opening_node1, opening_node2))))
        wall_openings_by_key[key].append((opening_index, row))

    def opening_interval(total, type_number, first, second):
        if type_number == 1:
            start, size = first, second
        elif type_number == 2:
            start, size = first - second, second
        elif type_number == 3:
            start, size = total - first - second, second
        elif type_number == 4:
            start, size = total - first, second
        elif type_number == 5:
            start, size = first, total - first - second
        else:
            raise ValueError(f"Unsupported wall-opening hold type: {type_number}")
        interpretation = "official-type"
        if size <= 0.0 and first > 0.0 and second >= 0.0:
            start, size = second, first
            interpretation = "zero-size-swapped"
        return start, size, interpretation

    matched_wall_opening_indexes = set()

    zero_thickness_walls = 0
    for row in sections.get("壁配置", []):
        if len(row) < 3:
            warning_counts["malformed wall rows skipped"] += 1
            continue
        story, symbol = clean(row[0]), clean(row[2])
        if story not in story_to_layers:
            warning_counts["wall rows with unknown stories skipped"] += 1
            continue
        node1, node2 = segment_from(row[1])
        bottom, top = story_to_layers[story]
        bottom_delta, bottom_source = segment_top_offset("WALLS bottom", bottom, node1, node2)
        top_delta, top_source = segment_top_offset("WALLS top", top, node1, node2)
        p1b, p2b = lift(node_point(bottom, node1), bottom_delta), lift(node_point(bottom, node2), bottom_delta)
        p1t, p2t = lift(node_point(top, node1), top_delta), lift(node_point(top, node2), top_delta)
        thickness = wall_sections.get(symbol, 0.0)
        opening_key = (story, tuple(sorted((node1, node2))))
        opening_records = []
        plan_length = math.hypot(p2b[0] - p1b[0], p2b[1] - p1b[1])
        wall_height = max(p1t[2], p2t[2]) - min(p1b[2], p2b[2])
        for opening_index, opening_row in wall_openings_by_key.get(opening_key, []):
            control = re.sub(r"\D", "", clean(opening_row[3]))
            if len(control) < 2:
                warning_counts["wall openings with unknown hold types skipped"] += 1
                continue
            try:
                type_x, type_y = int(control[0]), int(control[1])
                left, width, x_interpretation = opening_interval(
                    plan_length, type_x, number(opening_row[4]), number(opening_row[5])
                )
                opening_bottom, height, y_interpretation = opening_interval(
                    wall_height, type_y, number(opening_row[6]), number(opening_row[7])
                )
            except ValueError:
                warning_counts["wall openings with unsupported hold types skipped"] += 1
                continue
            right = left + width
            opening_top = opening_bottom + height
            if (
                width <= 0.0 or height <= 0.0 or left < -1e-6 or opening_bottom < -1e-6
                or right > plan_length + 1e-6 or opening_top > wall_height + 1e-6
            ):
                warning_counts["wall openings outside host walls skipped"] += 1
                continue
            record = {
                "identifier": clean(opening_row[2]),
                "hold_type": clean(opening_row[3]),
                "type_x": type_x,
                "type_y": type_y,
                "left_mm": max(0.0, left),
                "right_mm": min(plan_length, right),
                "bottom_mm": max(0.0, opening_bottom),
                "top_mm": min(wall_height, opening_top),
                "width_mm": width,
                "height_mm": height,
                "interpretation": {"x": x_interpretation, "y": y_interpretation},
                "weight_n_per_m2": number(opening_row[8]) if len(opening_row) > 8 else 0.0,
            }
            opening_records.append(record)
            matched_wall_opening_indexes.add(opening_index)
        try:
            vertices, faces = wall_mesh_with_openings(
                p1b, p2b, p2t, p1t, thickness, opening_records
            )
        except ValueError:
            warning_counts["zero-length walls skipped after same-node mapping"] += 1
            continue
        if thickness <= 0.0:
            zero_thickness_walls += 1
        placement_adjustments = {"level_follow": {
            "bottom_delta_mm": bottom_delta, "bottom_source": bottom_source,
            "top_delta_mm": top_delta, "top_source": top_source,
        }}
        if opening_records:
            placement_adjustments["wall_openings"] = opening_records
            wall_opening_counts["host_walls"] += 1
            wall_opening_counts["modeled"] += len(opening_records)
        add_object(
            "WALLS", story, symbol, vertices, faces, row[1], (thickness, 0.0),
            placement_adjustments=placement_adjustments,
        )
        centerlines.extend([
            {"kind": "WALLS", "level": story, "symbol": symbol, "p1": list(p1b), "p2": list(p2b)},
            {"kind": "WALLS", "level": story, "symbol": symbol, "p1": list(p1t), "p2": list(p2t)},
        ])

    unmatched_wall_opening_indexes = (
        set(range(len(wall_opening_rows))) - matched_wall_opening_indexes - malformed_opening_indexes
    )
    wall_opening_counts["unmatched_rows"] = len(unmatched_wall_opening_indexes)
    if unmatched_wall_opening_indexes:
        warning_counts["wall-opening rows not matched to modeled walls"] = len(unmatched_wall_opening_indexes)

    story_index = {story: index for index, story in enumerate(story_order)}
    offframe_wall_counts = Counter()
    zero_thickness_offframe_walls = 0
    for row in sections.get("フレーム外雑壁配置", []):
        offframe_wall_counts["input_rows"] += 1
        if len(row) < 10:
            warning_counts["malformed off-frame wall rows skipped"] += 1
            continue
        start_story, end_story = clean(row[3]), clean(row[4])
        if start_story not in story_index or end_story not in story_index:
            warning_counts["off-frame wall rows with unknown stories skipped"] += 1
            continue
        try:
            base_node = node_from(row[2])
        except ValueError:
            warning_counts["unparsed off-frame wall base nodes skipped"] += 1
            continue
        lower_index = min(story_index[start_story], story_index[end_story])
        upper_index = max(story_index[start_story], story_index[end_story])
        lower_story = story_order[lower_index]
        upper_story = story_order[upper_index]
        bottom_layer = story_to_layers[lower_story][0]
        top_layer = story_to_layers[upper_story][1]
        bottom_delta, bottom_source = node_top_offset("OFFFRAME_WALLS bottom", bottom_layer, base_node)
        top_delta, top_source = node_top_offset("OFFFRAME_WALLS top", top_layer, base_node)
        bottom_origin = lift(node_point(bottom_layer, base_node), bottom_delta)
        top_origin = lift(node_point(top_layer, base_node), top_delta)

        coordinate_values = [clean(value) for value in row[5:9]]
        if any(value.startswith("/") for value in coordinate_values):
            warning_counts["slash-style off-frame wall coordinates interpreted as relative offsets"] += 1
        start_x, start_y, end_x, end_y = [number(value.lstrip("/")) for value in coordinate_values]
        p1b = vadd(bottom_origin, (start_x, start_y, 0.0))
        p2b = vadd(bottom_origin, (end_x, end_y, 0.0))
        p1t = vadd(top_origin, (start_x, start_y, 0.0))
        p2t = vadd(top_origin, (end_x, end_y, 0.0))
        symbol = clean(row[9])
        thickness = offframe_wall_sections.get(symbol, 0.0)
        try:
            vertices, faces = wall_mesh(p1b, p2b, p2t, p1t, thickness)
        except ValueError:
            warning_counts["zero-length off-frame walls skipped"] += 1
            continue
        if thickness <= 0.0:
            zero_thickness_offframe_walls += 1
        level_label = start_story if start_story == end_story else f"{lower_story}-{upper_story}"
        adjustments = {
            "offframe_wall": {
                "base_node": list(base_node),
                "start_story": start_story,
                "end_story": end_story,
                "start_offset_xy_mm": [start_x, start_y],
                "end_offset_xy_mm": [end_x, end_y],
                "plan_start_mm": list(p1b[:2]),
                "plan_end_mm": list(p2b[:2]),
            },
            "level_follow": {
                "bottom_delta_mm": bottom_delta, "bottom_source": bottom_source,
                "top_delta_mm": top_delta, "top_source": top_source,
            },
        }
        add_object(
            "OFFFRAME_WALLS", level_label, symbol, vertices, faces, row[2],
            (thickness, 0.0), placement_adjustments=adjustments,
        )
        centerlines.extend([
            {"kind": "OFFFRAME_WALLS", "level": level_label, "symbol": symbol, "p1": list(p1b), "p2": list(p2b)},
            {"kind": "OFFFRAME_WALLS", "level": level_label, "symbol": symbol, "p1": list(p1t), "p2": list(p2t)},
        ])
        offframe_wall_counts["modeled"] += 1

    brace_groups = {}
    for row in sections.get("鉛直ブレース配置", []):
        if len(row) < 3 or clean(row[0]) not in story_index:
            warning_counts["malformed vertical brace rows skipped"] += 1
            continue
        story = clean(row[0])
        node1, node2 = segment_from(row[1])
        symbol = clean(row[2])
        brace_type = clean(row[3]) if len(row) > 3 else ""
        pair = clean(row[4]) if len(row) > 4 else ""
        key = (node1, node2, symbol, brace_type, pair)
        brace_groups.setdefault(key, []).append({"story": story, "frame": clean(row[1]), "row": row})

    brace_runs = []
    brace_merges = []
    for (node1, node2, symbol, brace_type, pair), entries in brace_groups.items():
        entries.sort(key=lambda entry: story_index[entry["story"]])
        current = [entries[0]]
        for entry in entries[1:]:
            previous = current[-1]
            boundary_layer = story_to_layers[previous["story"]][1]
            segment_key = tuple(sorted((node1, node2)))
            continues = (
                story_index[entry["story"]] == story_index[previous["story"]] + 1
                and (boundary_layer, segment_key) not in beam_keys
            )
            if continues:
                current.append(entry)
            else:
                brace_runs.append((node1, node2, symbol, brace_type, pair, current))
                current = [entry]
        brace_runs.append((node1, node2, symbol, brace_type, pair, current))

    for node1, node2, symbol, brace_type, pair, run in brace_runs:
        first_story, last_story = run[0]["story"], run[-1]["story"]
        bottom, top = story_to_layers[first_story][0], story_to_layers[last_story][1]
        p1b, p2b = node_point(bottom, node1), node_point(bottom, node2)
        p1t, p2t = node_point(top, node1), node_point(top, node2)
        section = brace_sections.get(symbol)
        if not section or section[0] <= 0 or section[1] <= 0:
            section = (50.0, 50.0, "fallback")
            fallback_sections[f"vertical brace {symbol}"] += len(run)
        level = first_story if first_story == last_story else f"{first_story}-{last_story}"
        diagonals, diagonal_nodes = [], []
        if "左" in pair or "両" in pair:
            diagonals.append((p1b, p2t))
            diagonal_nodes.append({"bottom_layer": bottom, "bottom_node": list(node1), "top_layer": top, "top_node": list(node2)})
        if "右" in pair or "両" in pair:
            diagonals.append((p2b, p1t))
            diagonal_nodes.append({"bottom_layer": bottom, "bottom_node": list(node2), "top_layer": top, "top_node": list(node1)})
        if not diagonals:
            diagonals = [(p1b, p2t), (p2b, p1t)]
            diagonal_nodes = [
                {"bottom_layer": bottom, "bottom_node": list(node1), "top_layer": top, "top_node": list(node2)},
                {"bottom_layer": bottom, "bottom_node": list(node2), "top_layer": top, "top_node": list(node1)},
            ]
        source = f"{run[0]['frame']} / {pair} / stories {first_story}-{last_story}"
        for p1, p2 in diagonals:
            add_linear("VBRACES", level, symbol, p1, p2, section[0], section[1], source=source)
        if len(run) > 1:
            brace_merges.append({
                "frame": run[0]["frame"], "symbol": symbol, "type": brace_type, "pair": pair,
                "stories": [entry["story"] for entry in run],
                "intermediate_layers_without_beam": [story_to_layers[entry["story"]][1] for entry in run[:-1]],
                "endpoint_nodes": diagonal_nodes,
                "bottom_endpoint": list(diagonals[0][0]), "top_endpoint": list(diagonals[0][1]),
            })

    for row in sections.get("水平ブレース配置", []):
        if len(row) < 2 or clean(row[0]) not in layer_z:
            warning_counts["malformed horizontal brace rows skipped"] += 1
            continue
        layer = clean(row[0])
        axes = [axis.upper() for axis in axes_in(row[1])]
        xs = [axis for axis in axes if axis_dir(axis) == "X"]
        ys = [axis for axis in axes if axis_dir(axis) == "Y"]
        if len(xs) != 2 or len(ys) != 2:
            warning_counts["unparsed horizontal brace areas skipped"] += 1
            continue
        symbol = clean(row[8]) if len(row) > 8 else ""
        pair = clean(row[9]) if len(row) > 9 else "両方"
        corners = [
            node_point(layer, (xs[0], ys[0])), node_point(layer, (xs[1], ys[0])),
            node_point(layer, (xs[1], ys[1])), node_point(layer, (xs[0], ys[1])),
        ]
        diagonals = []
        if "両" in pair or "1" in pair or "左" in pair:
            diagonals.append((corners[0], corners[2]))
        if "両" in pair or "2" in pair or "右" in pair:
            diagonals.append((corners[1], corners[3]))
        diameter = hbrace_diameters.get(symbol, 20.0)
        for p1, p2 in diagonals:
            add_linear("HBRACES", layer, symbol, p1, p2, diameter, diameter, shape="cylinder", source=f"{row[1]} / {pair}")

    slab_keys = set()
    duplicate_slabs = 0
    for row in sections.get("床配置", []):
        if len(row) < 2 or clean(row[0]) not in layer_z:
            warning_counts["malformed slab rows skipped"] += 1
            continue
        layer = clean(row[0])
        axes = [axis.upper() for axis in axes_in(row[1])]
        xs = [axis for axis in axes if axis_dir(axis) == "X"]
        ys = [axis for axis in axes if axis_dir(axis) == "Y"]
        if len(xs) != 2 or len(ys) != 2:
            warning_counts["unparsed slab areas skipped"] += 1
            continue
        symbol = next((clean(value) for value in reversed(row) if clean(value) in slab_sections), "")
        if not symbol:
            symbol = clean(row[9]) if len(row) > 9 else "UNKNOWN"
        key = (layer, tuple(xs), tuple(ys), symbol)
        if key in slab_keys:
            duplicate_slabs += 1
            continue
        slab_keys.add(key)
        slab_delta, slab_source = panel_top_offset("SLABS", layer, xs, ys)
        corners = [lift(node_point(layer, node), slab_delta) for node in (
            (xs[0], ys[0]), (xs[1], ys[0]), (xs[1], ys[1]), (xs[0], ys[1]),
        )]
        thickness = slab_sections.get(symbol, 0.0)
        if thickness <= 0.0:
            fallback_sections[f"slab {layer}/{symbol} (surface only)"] += 1
        try:
            vertices, faces = slab_mesh(corners, thickness)
        except ValueError:
            warning_counts["degenerate slab areas skipped after same-node mapping"] += 1
            continue
        add_object(
            "SLABS", layer, symbol, vertices, faces, row[1], (thickness, 0.0),
            placement_adjustments={"level_follow": {"delta_mm": slab_delta, "source": slab_source}},
        )

    cantilever_shape_rows = sections.get("片持床形状配置", []) + sections.get("片持床形状", [])
    cantilever_shapes = {}
    for row in cantilever_shape_rows:
        if len(row) >= 9:
            key = (clean(row[0]), clean(row[1]), clean(row[2]), clean(row[3]))
            cantilever_shapes[key] = row
        else:
            warning_counts["malformed cantilever slab shape rows skipped"] += 1

    cantilever_slab_counts = Counter()
    used_cantilever_shapes = set()
    for row in sections.get("片持床配置", []):
        cantilever_slab_counts["input_rows"] += 1
        if len(row) < 7 or clean(row[0]) not in layer_z:
            warning_counts["malformed cantilever slab rows skipped"] += 1
            continue
        layer = clean(row[0])
        key = (layer, clean(row[1]), clean(row[2]), clean(row[3]))
        shape = cantilever_shapes.get(key)
        if shape is None:
            warning_counts["cantilever slab rows without matching shapes skipped"] += 1
            continue
        try:
            node1, node2 = segment_from(row[1])
        except ValueError:
            warning_counts["unparsed cantilever slab support segments skipped"] += 1
            continue
        cantilever_delta, cantilever_source = segment_top_offset("CANTILEVER_SLABS", layer, node1, node2)
        p1, p2 = lift(node_point(layer, node1), cantilever_delta), lift(node_point(layer, node2), cantilever_delta)
        horizontal = (p2[0] - p1[0], p2[1] - p1[1], 0.0)
        try:
            tangent = vunit(horizontal)
        except ValueError:
            warning_counts["zero-length cantilever slab supports skipped"] += 1
            continue
        direction = clean(shape[4])
        if direction == "左":
            normal = (-tangent[1], tangent[0], 0.0)
        elif direction == "右":
            normal = (tangent[1], -tangent[0], 0.0)
        else:
            warning_counts["cantilever slab rows with unknown projection directions skipped"] += 1
            continue
        projection = number(shape[5])
        tip_move = number(shape[6])
        range_left = number(shape[7])
        range_right = number(shape[8])
        if projection <= 0.0:
            warning_counts["cantilever slabs with non-positive projections skipped"] += 1
            continue
        root_start = vsub(p1, vmul(tangent, range_left))
        root_end = vadd(p2, vmul(tangent, range_right))
        projection_vector = vmul(normal, projection)
        tip_shift = vmul(tangent, tip_move)
        tip_start = vadd(vadd(root_start, projection_vector), tip_shift)
        tip_end = vadd(vadd(root_end, projection_vector), tip_shift)
        corners = [root_start, root_end, tip_end, tip_start]
        symbol = clean(row[6])
        nominal, root_thickness, tip_thickness = cantilever_slab_sections.get(symbol, (0.0, 0.0, 0.0))
        if nominal <= 0.0 and root_thickness <= 0.0 and tip_thickness <= 0.0:
            fallback_sections[f"cantilever slab {layer}/{symbol} (surface only)"] += 1
        try:
            vertices, faces = tapered_cantilever_slab_mesh(corners, root_thickness, tip_thickness)
        except ValueError:
            warning_counts["degenerate cantilever slabs skipped"] += 1
            continue
        adjustments = {
            "cantilever_slab": {
                "support_segment": clean(row[1]),
                "projection_direction": direction,
                "projection_length_mm": projection,
                "tip_move_mm": tip_move,
                "range_left_mm": range_left,
                "range_right_mm": range_right,
                "root_start_mm": list(root_start),
                "root_end_mm": list(root_end),
                "tip_start_mm": list(tip_start),
                "tip_end_mm": list(tip_end),
            },
            "level_follow": {"delta_mm": cantilever_delta, "source": cantilever_source},
        }
        add_object(
            "CANTILEVER_SLABS", layer, symbol, vertices, faces, row[1],
            (nominal, root_thickness, tip_thickness), placement_adjustments=adjustments,
        )
        cantilever_slab_counts["modeled"] += 1
        used_cantilever_shapes.add(key)

    corner_slab_counts = Counter()
    for row in sections.get("出隅床配置", []):
        corner_slab_counts["input_rows"] += 1
        if len(row) < 8 or clean(row[0]) not in layer_z:
            warning_counts["malformed corner slab rows skipped"] += 1
            continue
        layer = clean(row[0])
        try:
            base_node = node_from(row[1])
        except ValueError:
            warning_counts["unparsed corner slab base nodes skipped"] += 1
            continue
        direction = clean(row[3]).upper()
        x_sign = 1.0 if "X+" in direction else -1.0 if "X-" in direction else 0.0
        y_sign = 1.0 if "Y+" in direction else -1.0 if "Y-" in direction else 0.0
        x_projection, y_projection = number(row[5]), number(row[6])
        if not x_sign or not y_sign or x_projection <= 0.0 or y_projection <= 0.0:
            warning_counts["corner slabs with invalid directions or projections skipped"] += 1
            continue
        tip_move = number(row[7])
        if abs(tip_move) > 1e-9:
            warning_counts["nonzero corner slab tip movements not applied"] += 1
        corner_delta, corner_source = node_top_offset("CORNER_SLABS", layer, base_node)
        base = lift(node_point(layer, base_node), corner_delta)
        x_tip = vadd(base, (x_sign * x_projection, 0.0, 0.0))
        outer = vadd(base, (x_sign * x_projection, y_sign * y_projection, 0.0))
        y_tip = vadd(base, (0.0, y_sign * y_projection, 0.0))
        symbol = clean(row[4])
        nominal, _, _ = cantilever_slab_sections.get(symbol, (0.0, 0.0, 0.0))
        if nominal <= 0.0:
            fallback_sections[f"corner slab {layer}/{symbol} (surface only)"] += 1
        try:
            vertices, faces = slab_mesh([base, x_tip, outer, y_tip], nominal)
        except ValueError:
            warning_counts["degenerate corner slabs skipped"] += 1
            continue
        adjustments = {
            "corner_slab": {
                "base_node": list(base_node),
                "projection_direction": direction,
                "x_projection_mm": x_projection,
                "y_projection_mm": y_projection,
                "tip_move_mm": tip_move,
                "base_mm": list(base),
                "outer_corner_mm": list(outer),
            },
            "level_follow": {"delta_mm": corner_delta, "source": corner_source},
        }
        add_object(
            "CORNER_SLABS", layer, symbol, vertices, faces, row[1],
            (nominal, 0.0), placement_adjustments=adjustments,
        )
        corner_slab_counts["modeled"] += 1

    unused_cantilever_shapes = set(cantilever_shapes) - used_cantilever_shapes
    if unused_cantilever_shapes:
        warning_counts["cantilever slab shape rows without matching placements"] = len(unused_cantilever_shapes)

    skipped_sections = []

    def section_row_count(*names):
        return sum(len(sections.get(name, [])) for name in names)

    unsupported_special_shapes = (
        (("セットバック",), "setback geometry requires a populated SS7 example to establish its CSV mapping"),
        (("節点回転移動", "節点の回転移動"), "node rotation movement is not implemented"),
        (("柱の回転",), "column section rotation is not implemented"),
    )
    for names, reason in unsupported_special_shapes:
        count = section_row_count(*names)
        if count:
            skipped_sections.append({"section": names[0], "row_count": count, "reason": reason})

    member_offset_rows = sections.get("部材の寄り", [])
    nondefault_member_offset_rows = 0
    for row in member_offset_rows:
        control = clean(row[1]) if len(row) > 1 else ""
        dimensions = [number(row[index]) for index in range(2, min(5, len(row)))]
        if control not in ("", "中心") or any(abs(value) > 1e-9 for value in dimensions):
            nondefault_member_offset_rows += 1
    if nondefault_member_offset_rows:
        skipped_sections.append({
            "section": "部材の寄り",
            "row_count": nondefault_member_offset_rows,
            "reason": "non-default column/beam/wall offsets require a populated reference model for direction validation",
        })

    special_shape_support = [
        {"item": "3.1 節点移動（軸振れ）", "status": "implemented", "input_rows": len(sections.get("軸振れ", []))},
        {"item": "3.2 セットバック", "status": "not-present" if not section_row_count("セットバック") else "detected-not-expanded", "input_rows": section_row_count("セットバック")},
        {"item": "3.3 節点上下移動", "status": "implemented", "input_rows": len(sections.get("節点の上下移動", []))},
        {"item": "3.4 節点回転移動", "status": "not-present" if not section_row_count("節点回転移動", "節点の回転移動") else "detected-not-expanded", "input_rows": section_row_count("節点回転移動", "節点の回転移動")},
        {"item": "3.5 節点同一化", "status": "implemented", "input_rows": len(sections.get("節点の同一化", []))},
        {"item": "3.6 柱の回転", "status": "not-present" if not section_row_count("柱の回転") else "detected-not-expanded", "input_rows": section_row_count("柱の回転")},
        {
            "item": "3.7 部材の寄り",
            "status": "centered-zero-only" if member_offset_rows and not nondefault_member_offset_rows else "not-present" if not member_offset_rows else "detected-not-expanded",
            "input_rows": len(member_offset_rows),
            "nondefault_rows": nondefault_member_offset_rows,
        },
        {
            "item": "3.8 梁のレベル調整", "status": "implemented",
            "input_rows": len(sections.get("梁のレベル調整", [])) + beam_level_override_rows,
        },
    ]

    if duplicate_slabs:
        warning_counts["duplicate slab placements deduplicated"] = duplicate_slabs
    if zero_thickness_walls:
        warning_counts["walls exported as surfaces because thickness is blank or zero"] = zero_thickness_walls
    if zero_thickness_offframe_walls:
        warning_counts["off-frame walls exported as surfaces because thickness is blank or zero"] = zero_thickness_offframe_walls
    if not objects:
        raise ValueError("No supported structural objects were found in the CSV")

    all_vertices = [vertex for obj in objects for vertex in obj["vertices"]]
    bbox_min = [min(v[i] for v in all_vertices) for i in range(3)]
    bbox_max = [max(v[i] for v in all_vertices) for i in range(3)]
    project_name = ""
    for row in all_rows:
        if row and clean(row[0]) == "物件名" and len(row) > 1:
            project_name = clean(row[1])
            break
    return {
        "schema": "ss7-rhino-model-v7",
        "source_csv": str(csv_path.resolve()),
        "source_encoding": source_encoding,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "project_name": project_name,
        "units": "millimeters",
        "coordinate_system": "X axis spans -> Rhino X, Y axis spans -> Rhino Y, SS7 levels -> Rhino Z",
        "preflight": {
            "stories_bottom_up": story_order,
            "layers_bottom_up": list(reversed(layers_top_down)),
            "x_axis_count": len(x_axes), "y_axis_count": len(y_axes),
            "required_sections_ok": True,
        },
        "axis_positions": {"X": x_pos, "Y": y_pos},
        "layer_elevations": layer_z,
        "node_corrections": {
            "axis_shifts": len(shifts), "vertical_moves": len(z_moves), "same_node_mappings": len(same_nodes),
        },
        "modeling_adjustments": {
            "beam_levels": {
                "layer_default_rows": len(beam_level_defaults),
                "individual_input_rows": beam_level_override_rows,
                "individual_parsed_keys": len(beam_level_overrides),
                "individual_matched_keys": len(matched_beam_level_keys),
                "individual_unmatched_keys": len(unmatched_beam_level_keys),
                "applied_beam_count": sum(beam_level_source_counts.values()),
                "source_counts": dict(beam_level_source_counts),
                "control_counts": dict(beam_level_control_counts),
            },
            "column_lengths": {
                "column_count": column_length_counts["columns"],
                "endpoints_with_incident_beams": column_length_counts["endpoints_with_incident_beams"],
                "columns_extended": column_length_counts["columns_extended"],
                "bottom_endpoints_extended": column_length_counts["bottom_endpoints_extended"],
                "top_endpoints_extended": column_length_counts["top_endpoints_extended"],
                "total_extension_mm": round(column_extension_total_mm, 6),
                "max_endpoint_extension_mm": round(column_max_endpoint_extension_mm, 6),
                "bottom_endpoints_shortened": column_length_counts["bottom_endpoints_shortened"],
                "top_endpoints_shortened": column_length_counts["top_endpoints_shortened"],
                "remaining_separated_endpoints": column_length_counts["remaining_separated_endpoints"],
            },
            "level_follow": dict(sorted(level_follow_counts.items())),
            "supplemental_elements": {
                "offframe_wall_input_rows": offframe_wall_counts["input_rows"],
                "offframe_walls_modeled": offframe_wall_counts["modeled"],
                "cantilever_slab_shape_rows": len(cantilever_shapes),
                "cantilever_slab_input_rows": cantilever_slab_counts["input_rows"],
                "cantilever_slabs_modeled": cantilever_slab_counts["modeled"],
                "corner_slab_input_rows": corner_slab_counts["input_rows"],
                "corner_slabs_modeled": corner_slab_counts["modeled"],
            },
            "floor_framing": {
                "small_beam_input_rows": small_beam_counts["input_rows"],
                "small_beams_modeled": small_beam_counts["modeled"],
                "small_beam_unmatched_rows": small_beam_counts["unmatched_rows"],
                "floor_assembly_input_rows": small_beam_counts["floor_assembly_input_rows"],
                "floor_assemblies_expanded": small_beam_counts["floor_assemblies_expanded"],
                "floor_shape_rows": small_beam_counts["floor_shape_rows"],
            },
            "wall_openings": {
                "input_rows": wall_opening_counts["input_rows"],
                "modeled": wall_opening_counts["modeled"],
                "host_walls": wall_opening_counts["host_walls"],
                "unmatched_rows": wall_opening_counts["unmatched_rows"],
            },
            "member_offsets": {
                "input_rows": len(member_offset_rows),
                "centered_zero_rows": len(member_offset_rows) - nondefault_member_offset_rows,
                "nondefault_rows": nondefault_member_offset_rows,
            },
            "special_shape_support": special_shape_support,
        },
        "bbox": {"min": bbox_min, "max": bbox_max},
        "object_counts": dict(Counter(obj["kind"] for obj in objects)),
        "vertical_brace_continuity": {
            "merged_runs": len(brace_merges),
            "merged_input_rows": sum(len(record["stories"]) for record in brace_merges),
            "records": brace_merges,
        },
        "fallback_sections": [
            {"member": name, "row_count": count} for name, count in sorted(fallback_sections.items())
        ],
        "warnings": [
            {"message": message, "row_count": count} for message, count in sorted(warning_counts.items())
        ],
        "skipped_sections": skipped_sections,
        "objects": objects,
        "centerlines": centerlines,
        "assumptions": [
            "Steel H/box/pipe catalog strings are represented by their overall bounding width and depth.",
            "For vertical X braces, pair left is bottom-start to top-end and pair right is bottom-end to top-start.",
            "Consecutive vertical-brace inputs are merged when the same span has no girder at the intermediate layer.",
            "Beam solids use the SS7 standard layer as the beam-top reference; positive level dimensions are upward.",
            "Beam level control is converted to centerline Z using the actual member depth (top, center, or bottom control).",
            "Column ends are extended only when needed to meet moved incident beam faces; ordinary column lengths are unchanged.",
            "Cantilever slab left/right projection is resolved from the directed SS7 support segment; root and tip thicknesses are preserved.",
            "Corner slabs are expanded from the SS7 base node and signed X/Y projection directions.",
            "Off-frame wall start/end XY values are treated as offsets from the selected SS7 base node across the specified story range.",
            "Small beams are expanded recursively from floor-assembly shapes and clipped to the rotated bounding rectangle of each target floor region.",
            "Small-beam centerlines are placed half the registered section depth below the floor top reference.",
            "Wall openings are removed from the wall mesh using the two-digit X/Y hold type and dimensions from the wall's external bounding rectangle.",
            "When an opening's official type dimension is zero but its paired value is positive, the paired values are treated as zero offset and positive opening size.",
            "Horizontal turnbuckle brace diameter is taken from the TB-M number for display geometry.",
            "The output is a geometry/audit model, not a round-trip SS7 analysis model.",
        ],
    }


COLORS = {
    "COLUMNS": (55, 170, 90),
    "BEAMS": (70, 125, 225),
    "SMALL_BEAMS": (70, 185, 235),
    "VBRACES": (225, 70, 70),
    "WALLS": (235, 185, 65),
    "OFFFRAME_WALLS": (225, 125, 55),
    "HBRACES": (185, 80, 205),
    "SLABS": (105, 175, 185),
    "CANTILEVER_SLABS": (60, 185, 210),
    "CORNER_SLABS": (70, 195, 155),
}
DXF_COLORS = {
    "COLUMNS": 3, "BEAMS": 5, "SMALL_BEAMS": 4, "VBRACES": 1, "WALLS": 2, "OFFFRAME_WALLS": 30,
    "HBRACES": 6, "SLABS": 4, "CANTILEVER_SLABS": 4, "CORNER_SLABS": 3,
}


def kind_from_layer(layer_name: str):
    return next(
        (kind for kind in sorted(COLORS, key=len, reverse=True) if layer_name.startswith(kind + "_")),
        layer_name.split("_", 1)[0],
    )


def write_obj(model: dict, path: Path):
    mtl_path = path.with_suffix(".mtl")
    lines = [f"mtllib {mtl_path.name}", "# SS7 3D structural model; coordinates are millimetres."]
    vertex_offset = 1
    for obj in model["objects"]:
        lines.append(f"o {obj['name']}")
        lines.append(f"g {obj['layer']}")
        lines.append(f"usemtl {obj['kind']}")
        lines.append(f"# SS7 symbol={obj['symbol']} source={obj['source']}")
        for x, y, z in obj["vertices"]:
            lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")
        for face in obj["faces"]:
            indexes = [str(vertex_offset + index) for index in face]
            lines.append("f " + " ".join(indexes))
        vertex_offset += len(obj["vertices"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    mtl_lines = ["# SS7 material colors"]
    for kind, rgb in COLORS.items():
        r, g, b = (value / 255.0 for value in rgb)
        opacity = 0.35 if kind in ("SLABS", "CANTILEVER_SLABS", "CORNER_SLABS") else 0.45 if kind in ("WALLS", "OFFFRAME_WALLS") else 1.0
        mtl_lines.extend([f"newmtl {kind}", f"Kd {r:.4f} {g:.4f} {b:.4f}", f"d {opacity:.3f}", "illum 1", ""])
    mtl_path.write_text("\n".join(mtl_lines), encoding="ascii")


def dxf_pairs(pairs):
    return "".join(f"{code}\n{value}\n" for code, value in pairs)


def write_dxf(model: dict, path: Path):
    layers = sorted({obj["layer"] for obj in model["objects"]})
    minp = model["bbox"]["min"]
    maxp = model["bbox"]["max"]
    chunks = [dxf_pairs([
        (0, "SECTION"), (2, "HEADER"),
        (9, "$ACADVER"), (1, "AC1015"),
        (9, "$INSUNITS"), (70, 4),
        (9, "$EXTMIN"), (10, minp[0]), (20, minp[1]), (30, minp[2]),
        (9, "$EXTMAX"), (10, maxp[0]), (20, maxp[1]), (30, maxp[2]),
        (0, "ENDSEC"),
        (0, "SECTION"), (2, "TABLES"),
        (0, "TABLE"), (2, "LAYER"), (70, len(layers)),
    ])]
    for layer in layers:
        kind = kind_from_layer(layer)
        chunks.append(dxf_pairs([(0, "LAYER"), (2, layer), (70, 0), (62, DXF_COLORS.get(kind, 7)), (6, "CONTINUOUS")]))
    chunks.append(dxf_pairs([(0, "ENDTAB"), (0, "ENDSEC"), (0, "SECTION"), (2, "ENTITIES")]))

    for obj in model["objects"]:
        vertices = obj["vertices"]
        faces = obj["faces"]
        layer = obj["layer"]
        comment = f"{obj['name']} | {obj['symbol']} | {obj['source']}".encode("ascii", "replace").decode("ascii")
        chunks.append(dxf_pairs([(999, comment), (0, "POLYLINE"), (8, layer), (66, 1), (70, 64), (71, len(vertices)), (72, len(faces)), (10, 0), (20, 0), (30, 0)]))
        for x, y, z in vertices:
            chunks.append(dxf_pairs([(0, "VERTEX"), (8, layer), (10, x), (20, y), (30, z), (70, 192)]))
        for face in faces:
            face_pairs = [(0, "VERTEX"), (8, layer), (10, 0), (20, 0), (30, 0), (70, 128)]
            for code, index in zip((71, 72, 73, 74), face):
                face_pairs.append((code, index + 1))
            chunks.append(dxf_pairs(face_pairs))
        chunks.append(dxf_pairs([(0, "SEQEND"), (8, layer)]))

    chunks.append(dxf_pairs([(0, "ENDSEC"), (0, "EOF")]))
    path.write_text("".join(chunks), encoding="ascii", newline="\n")


def write_preview(model: dict, path: Path):
    from PIL import Image, ImageDraw, ImageFont

    width, height = 1800, 1250
    margin = 90

    def project(point):
        x, y, z = point
        return ((x - y) * 0.8660254, z + (x + y) * 0.38)

    points_2d = [project(vertex) for obj in model["objects"] for vertex in obj["vertices"]]
    min_x = min(p[0] for p in points_2d)
    max_x = max(p[0] for p in points_2d)
    min_y = min(p[1] for p in points_2d)
    max_y = max(p[1] for p in points_2d)
    scale = min((width - 2 * margin) / (max_x - min_x), (height - 2 * margin) / (max_y - min_y))

    def screen(point):
        px, py = project(point)
        return (margin + (px - min_x) * scale, height - margin - (py - min_y) * scale)

    image = Image.new("RGB", (width, height), (249, 250, 252))
    draw = ImageDraw.Draw(image, "RGBA")
    try:
        title_font = ImageFont.truetype(r"C:\Windows\Fonts\meiryo.ttc", 18)
        note_font = ImageFont.truetype(r"C:\Windows\Fonts\meiryo.ttc", 13)
    except OSError:
        title_font = ImageFont.load_default()
        note_font = ImageFont.load_default()

    slab_kinds = {"SLABS", "CANTILEVER_SLABS", "CORNER_SLABS"}
    wall_kinds = {"WALLS", "OFFFRAME_WALLS"}
    for obj in model["objects"]:
        if obj["kind"] not in slab_kinds | wall_kinds:
            continue
        alpha = 38 if obj["kind"] in slab_kinds else 75
        color = COLORS[obj["kind"]] + (alpha,)
        for face in obj["faces"]:
            draw.polygon(
                [screen(obj["vertices"][i]) for i in face],
                fill=color,
                outline=COLORS[obj["kind"]] + (100 if obj["kind"] in slab_kinds else 145,),
            )

    line_widths = {
        "COLUMNS": 5, "BEAMS": 4, "SMALL_BEAMS": 3, "VBRACES": 3, "HBRACES": 2,
        "WALLS": 2, "OFFFRAME_WALLS": 3, "SLABS": 1,
        "CANTILEVER_SLABS": 1, "CORNER_SLABS": 1,
    }
    order = {
        "SLABS": 0, "CANTILEVER_SLABS": 0, "CORNER_SLABS": 0,
        "HBRACES": 1, "WALLS": 2, "OFFFRAME_WALLS": 2,
        "BEAMS": 3, "SMALL_BEAMS": 3, "COLUMNS": 4, "VBRACES": 5,
    }
    for line in sorted(model["centerlines"], key=lambda item: order.get(item["kind"], 0)):
        color = COLORS[line["kind"]] + (230,)
        draw.line([screen(line["p1"]), screen(line["p2"])], fill=color, width=line_widths[line["kind"]])

    draw.rectangle((0, 0, width, 56), fill=(32, 39, 54, 245))
    counts = model["object_counts"]
    title = f"SS7 -> Rhino 3D preview | {model['project_name']} | " + ", ".join(f"{k}:{v}" for k, v in counts.items())
    draw.text((24, 17), title, fill=(255, 255, 255, 255), font=title_font)
    draw.text((24, height - 36), "Units: mm | Axis shifts, vertical moves, same-node mappings, suffix axes", fill=(48, 56, 72, 255), font=note_font)
    image.save(path)


def load_rhino3dm():
    """Load a functional rhino3dm, preferring the project-bundled package."""
    module = None
    try:
        module = importlib.import_module("rhino3dm")
    except ImportError:
        pass
    if module is not None and hasattr(module, "File3dm"):
        return module

    project_dir = Path(__file__).resolve().parents[1]
    vendor_candidates = []
    configured_vendor = os.environ.get("SS7_RHINO3DM_VENDOR")
    if configured_vendor:
        vendor_candidates.append(Path(configured_vendor))
    vendor_candidates.extend([
        project_dir / "outputs" / "ss7_converter_ui" / "vendor",
        project_dir / "work" / "vendor",
    ])
    checked_vendors = []
    for vendor_dir in vendor_candidates:
        vendor_dir = vendor_dir.resolve()
        if vendor_dir in checked_vendors:
            continue
        checked_vendors.append(vendor_dir)
        package_dir = vendor_dir / "rhino3dm"
        package_init = package_dir / "__init__.py"
        try:
            package_available = package_init.is_file()
        except OSError:
            continue
        if not package_available:
            continue
        try:
            vendor_text = str(vendor_dir)
            sys.path[:] = [entry for entry in sys.path if str(entry) != vendor_text]
            sys.path.insert(0, vendor_text)
            for name in list(sys.modules):
                if name == "rhino3dm" or name.startswith("rhino3dm."):
                    del sys.modules[name]
            importlib.invalidate_caches()
            spec = importlib.util.spec_from_file_location(
                "rhino3dm",
                package_init,
                submodule_search_locations=[str(package_dir)],
            )
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                sys.modules["rhino3dm"] = module
                spec.loader.exec_module(module)
            if module is not None and hasattr(module, "File3dm"):
                return module
        except (ImportError, OSError):
            module = None
            continue
    if module is None or not hasattr(module, "File3dm"):
        raise ImportError("A functional rhino3dm package was not found")
    return module


def write_3dm(model: dict, path: Path, verify_path: Path):
    try:
        rhino3dm = load_rhino3dm()
    except ImportError as error:
        return {"written": False, "valid": False, "reason": str(error)}

    file3dm = rhino3dm.File3dm()
    file3dm.Settings.ModelUnitSystem = rhino3dm.UnitSystem.Millimeters
    file3dm.Settings.ModelAbsoluteTolerance = 0.01
    file3dm.Settings.ModelAngleToleranceDegrees = 1.0
    layer_indexes = {}
    for layer_name in sorted({obj["layer"] for obj in model["objects"]}):
        kind = kind_from_layer(layer_name)
        layer = rhino3dm.Layer()
        layer.Name = layer_name
        layer.Color = COLORS.get(kind, (180, 180, 180)) + (255,)
        layer_indexes[layer_name] = file3dm.Layers.Add(layer)

    for obj in model["objects"]:
        mesh = rhino3dm.Mesh()
        for x, y, z in obj["vertices"]:
            mesh.Vertices.Add(float(x), float(y), float(z))
        for face in obj["faces"]:
            if len(face) == 3:
                mesh.Faces.AddFace(int(face[0]), int(face[1]), int(face[2]))
            elif len(face) == 4:
                mesh.Faces.AddFace(int(face[0]), int(face[1]), int(face[2]), int(face[3]))
            else:
                raise ValueError(f"Unsupported mesh face with {len(face)} vertices in {obj['name']}")
        mesh.Normals.ComputeNormals()
        mesh.Compact()
        attributes = rhino3dm.ObjectAttributes()
        attributes.Name = obj["name"]
        attributes.LayerIndex = layer_indexes[obj["layer"]]
        attributes.SetUserString("SS7.Kind", obj["kind"])
        attributes.SetUserString("SS7.Level", obj["level"])
        attributes.SetUserString("SS7.Symbol", obj["symbol"])
        attributes.SetUserString("SS7.Source", obj["source"])
        attributes.SetUserString("SS7.SectionMM", "x".join(str(v) for v in obj["section_mm"]))
        adjustments = obj.get("placement_adjustments", {})
        if adjustments:
            attributes.SetUserString("SS7.PlacementAdjustments", json.dumps(adjustments, ensure_ascii=False))
        if obj["kind"] == "BEAMS" and adjustments:
            attributes.SetUserString("SS7.BeamLevelControl", str(adjustments["beam_level_control"]))
            attributes.SetUserString("SS7.BeamLevelDimensionMM", str(adjustments["beam_level_dimension_mm"]))
            attributes.SetUserString("SS7.BeamCenterOffsetZMM", str(adjustments["beam_center_offset_z_mm"]))
            attributes.SetUserString("SS7.BeamLevelSource", str(adjustments["beam_level_source"]))
        if obj["kind"] == "COLUMNS" and adjustments.get("column_length"):
            column_length = adjustments["column_length"]
            attributes.SetUserString("SS7.ColumnBottomExtensionMM", str(column_length["bottom_extension_mm"]))
            attributes.SetUserString("SS7.ColumnTopExtensionMM", str(column_length["top_extension_mm"]))
        file3dm.Objects.AddMesh(mesh, attributes)

    if not file3dm.Write(str(path), 8):
        raise RuntimeError("File3dm.Write returned false")
    check = rhino3dm.File3dm.Read(str(path))
    if check is None:
        raise RuntimeError("The written 3DM could not be reopened")
    invalid_count = 0
    open_mesh_count = 0
    for file_object in check.Objects:
        geometry = file_object.Geometry
        if hasattr(geometry, "IsValid") and not geometry.IsValid:
            invalid_count += 1
        if hasattr(geometry, "IsClosed") and not geometry.IsClosed:
            open_mesh_count += 1
    verification = {
        "written": True,
        "path": str(path.resolve()),
        "object_count_expected": len(model["objects"]),
        "object_count_readback": len(check.Objects),
        "layer_count_readback": len(check.Layers),
        "units_readback": str(check.Settings.ModelUnitSystem),
        "invalid_geometry_count": invalid_count,
        "open_mesh_count": open_mesh_count,
        "valid": len(check.Objects) == len(model["objects"]) and invalid_count == 0,
    }
    verify_path.write_text(json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8")
    return verification


def write_report(model: dict, path: Path, source: Path, artifacts: dict, verification: dict):
    counts = model["object_counts"]
    bbox = model["bbox"]
    corrections = model["node_corrections"]
    adjustments = model["modeling_adjustments"]
    beam_levels = adjustments["beam_levels"]
    column_lengths = adjustments["column_lengths"]
    supplemental = adjustments["supplemental_elements"]
    floor_framing = adjustments["floor_framing"]
    wall_openings = adjustments["wall_openings"]
    continuity = model["vertical_brace_continuity"]
    preflight = model["preflight"]
    lines = [
        "# SS7 → Rhino 3D 変換レポート",
        "",
        f"- 元データ: `{source}`",
        f"- 物件名: {model['project_name']}",
        f"- 読込文字コード: {model['source_encoding']}",
        "- 座標単位: mm",
        "- 座標対応: SS7 X → Rhino X、SS7 Y → Rhino Y、階高 → Rhino Z",
        f"- 階: {', '.join(preflight['stories_bottom_up'])}",
        f"- 層: {', '.join(preflight['layers_bottom_up'])}",
        f"- 通り芯: X {preflight['x_axis_count']} 本、Y {preflight['y_axis_count']} 本",
        f"- 外形範囲: X {bbox['min'][0]:.1f}～{bbox['max'][0]:.1f}, Y {bbox['min'][1]:.1f}～{bbox['max'][1]:.1f}, Z {bbox['min'][2]:.1f}～{bbox['max'][2]:.1f}",
        "",
        "## 生成オブジェクト",
        "",
        f"- 柱: {counts.get('COLUMNS', 0)}",
        f"- 梁: {counts.get('BEAMS', 0)}",
        f"- 小梁: {counts.get('SMALL_BEAMS', 0)}",
        f"- 鉛直ブレース: {counts.get('VBRACES', 0)}",
        f"- 壁: {counts.get('WALLS', 0)}",
        f"- フレーム外雑壁: {counts.get('OFFFRAME_WALLS', 0)}",
        f"- 水平ブレース: {counts.get('HBRACES', 0)}",
        f"- 床: {counts.get('SLABS', 0)}",
        f"- 片持床: {counts.get('CANTILEVER_SLABS', 0)}",
        f"- 出隅床: {counts.get('CORNER_SLABS', 0)}",
        f"- 鉛直ブレース連続統合: {continuity['merged_runs']} 組（入力 {continuity['merged_input_rows']} 行）",
        "",
        "## 反映した節点補正",
        "",
        f"- 軸振れ: {corrections['axis_shifts']} 箇所",
        f"- 節点の上下移動: {corrections['vertical_moves']} 箇所",
        f"- 節点の同一化: {corrections['same_node_mappings']} 箇所",
        "",
        "## 梁のレベル調整",
        "",
        f"- 層標準設定: {beam_levels['layer_default_rows']} 行",
        f"- 大梁個別設定: 入力 {beam_levels['individual_input_rows']} 行 / 梁と一致 {beam_levels['individual_matched_keys']} 行 / 不一致 {beam_levels['individual_unmatched_keys']} 行",
        f"- レベルを反映した梁: {beam_levels['applied_beam_count']} 本",
        f"- 適用元: {beam_levels['source_counts']}",
        f"- 押さえ: {beam_levels['control_counts']}",
        "- SS7層レベルを梁上端の基準とし、上面・中心・下面の押さえと部材せいから梁中心Zを算出した。",
        "- レベル調整値は正を上方向、負を下方向として反映した。",
        "",
        "## 梁レベルへの追従（柱・壁・床・小梁）",
        "",
        f"- 接続梁を確認した柱端部: {column_lengths['endpoints_with_incident_beams']} 箇所",
        f"- 長さが変わった柱: {column_lengths['columns_extended']} 本",
        f"- 柱脚: 下へ延長 {column_lengths['bottom_endpoints_extended']} 箇所 / 上へ短縮 {column_lengths.get('bottom_endpoints_shortened', 0)} 箇所",
        f"- 柱頭: 上へ延長 {column_lengths['top_endpoints_extended']} 箇所 / 下へ短縮 {column_lengths.get('top_endpoints_shortened', 0)} 箇所",
        f"- 最大変化量: {column_lengths['max_endpoint_extension_mm']:.1f} mm",
        "- 柱頭は節点に接する大梁の上端の最高位置、柱脚は下の節点の大梁上端の最高位置に合わせた。最下層の柱脚は基礎梁の下端（接する梁の下端の最低位置）まで伸ばした。",
        "- 壁の上下端は同じ区間の大梁の上端、床と床組（小梁）は周囲の大梁の上端の最も低い位置、片持床は支持する大梁、出隅床とフレーム外雑壁は基準節点の大梁に合わせた。大梁がない場合は層の梁レベル標準値（上面押さえのとき）を使った。",
        f"- 決め方の内訳: {adjustments.get('level_follow', {})}",
        "",
        "## 片持床・出隅床・フレーム外雑壁",
        "",
        f"- フレーム外雑壁: 入力 {supplemental['offframe_wall_input_rows']} 行 / モデル化 {supplemental['offframe_walls_modeled']} 枚",
        f"- 片持床: 形状 {supplemental['cantilever_slab_shape_rows']} 行 / 配置 {supplemental['cantilever_slab_input_rows']} 行 / モデル化 {supplemental['cantilever_slabs_modeled']} 枚",
        f"- 出隅床: 入力 {supplemental['corner_slab_input_rows']} 行 / モデル化 {supplemental['corner_slabs_modeled']} 枚",
        "- 片持床は支持辺の向き、左右、跳出し長さ、先端移動、左右範囲、元端・先端厚を形状へ反映した。",
        "- 出隅床は基点とX/Yの符号付き跳出し長さ、フレーム外雑壁は基点からの始終点XYと配置階範囲を形状へ反映した。",
        "",
        "## 小梁・床組",
        "",
        f"- 小梁配置: 入力 {floor_framing['small_beam_input_rows']} 行 / モデル化 {floor_framing['small_beams_modeled']} 本 / 未一致 {floor_framing['small_beam_unmatched_rows']} 行",
        f"- 床組配置: 入力 {floor_framing['floor_assembly_input_rows']} 行 / 小梁階層を展開 {floor_framing['floor_assemblies_expanded']} 面",
        f"- 床組形状: {floor_framing['floor_shape_rows']} 行",
        "- 床組形状のX/Y方向、本数、間隔、角度、子床組を再帰展開し、1～5次の小梁配置パスに対応させた。",
        "",
        "## 壁開口",
        "",
        f"- 壁開口配置: 入力 {wall_openings['input_rows']} 行 / 穴あけ {wall_openings['modeled']} 箇所 / 対象壁 {wall_openings['host_walls']} 枚 / 未一致 {wall_openings['unmatched_rows']} 行",
        "- 開口矩形内の壁メッシュを除去し、壁厚がある場合は周囲を閉じたパネルとして3DM化した。",
        "",
        "## 建物特殊形状 3.1～3.8 対応監査",
        "",
    ]
    status_labels = {
        "implemented": "反映済み",
        "not-present": "今回のCSVに入力なし",
        "centered-zero-only": "全行が中心・0 mm（形状変化なし）",
        "detected-not-expanded": "入力を検出したが未展開",
    }
    for item in adjustments["special_shape_support"]:
        details = f"（入力 {item['input_rows']} 行"
        if "nondefault_rows" in item:
            details += f"、非標準 {item['nondefault_rows']} 行"
        details += "）"
        lines.append(f"- {item['item']}: {status_labels.get(item['status'], item['status'])} {details}")
    lines.extend([
        "",
        "## 判定・制限事項",
        "",
        "- 鉛直Xブレースは「片(左)」を左下→右上、「片(右)」を右下→左上として作成した。",
        "- 水平ブレースは登録形状のTB-M径を表示径として使用した。",
        "- 同じスパンの連続階に鉛直ブレース入力があり、中間層に大梁がない場合は1本へ統合した。",
        "- H形鋼・角形鋼管・鋼管は外形幅×外形せいの表示用ソリッドとして作成した。",
        "- 形状確認用モデルであり、SS7へ戻す往復変換や解析モデルの完全互換は対象外。",
    ])
    if model["fallback_sections"]:
        lines.extend(["", "### 仮断面を使った部材", ""])
        for item in model["fallback_sections"]:
            lines.append(f"- {item['member']}: {item['row_count']} 行")
    if model["warnings"]:
        lines.extend(["", "### 警告・自動整理", ""])
        for item in model["warnings"]:
            lines.append(f"- {item['message']}: {item['row_count']} 行")
    if model["skipped_sections"]:
        lines.extend(["", "### 未展開のSS7項目", ""])
        for item in model["skipped_sections"]:
            lines.append(f"- {item['section']}: {item['row_count']} 行 — {item['reason']}")
    if continuity["records"]:
        lines.extend(["", "## 鉛直ブレースの連続判定", "", f"- 中間梁なしで統合した連続ブレース: {continuity['merged_runs']} 組"])
        for record in continuity["records"]:
            stories = "～".join(record["stories"])
            layers = ", ".join(record["intermediate_layers_without_beam"])
            lines.append(f"- {record['frame']} / {stories} / {record['pair']}（{layers}に大梁なし）")
    lines.extend([
        "",
        "## Rhinoでの使用",
        "",
        f"1. `{Path(artifacts['3dm']).name}` を直接開く。" if artifacts.get("3dm") else "1. 3DMは未生成（rhino3dmが必要）。",
        f"2. 3DMがない場合は `{Path(artifacts['dxf']).name}` を開く（単位はmm）。",
        "3. OBJを使う場合はインポート単位をmmに指定する。",
    ])
    lines.extend([
        "",
        "## 3DM再読込検証",
        "",
        f"- 書き出しオブジェクト数: {verification.get('object_count_expected', '未生成')}",
        f"- 再読込オブジェクト数: {verification.get('object_count_readback', '未生成')}",
        f"- レイヤー数: {verification.get('layer_count_readback', '未生成')}",
        f"- 単位: {verification.get('units_readback', '未生成')}",
        f"- 無効形状: {verification.get('invalid_geometry_count', '未検証')}",
        f"- 開いたメッシュ: {verification.get('open_mesh_count', '未検証')}（壁厚なしの壁面を含む）",
        f"- 検証結果: {'正常' if verification.get('valid') else verification.get('reason', '不一致')}",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Convert an SS7 all-items CSV to Rhino-friendly 3D files")
    parser.add_argument("source_csv", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--prefix", help="Output filename prefix (default: sanitized CSV stem)")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", args.prefix or args.source_csv.stem).strip("_") or "ss7"

    model = make_model(args.source_csv)
    json_path = args.output_dir / f"{prefix}_model_data.json"
    obj_path = args.output_dir / f"{prefix}_rhino_model.obj"
    dxf_path = args.output_dir / f"{prefix}_rhino_model.dxf"
    preview_path = args.output_dir / f"{prefix}_rhino_preview.png"
    model_path = args.output_dir / f"{prefix}_rhino_model.3dm"
    verify_path = args.output_dir / f"{prefix}_3dm_verify.json"
    report_path = args.output_dir / f"{prefix}_conversion_report.md"
    json_path.write_text(json.dumps(model, ensure_ascii=False, indent=2), encoding="utf-8")
    write_obj(model, obj_path)
    write_dxf(model, dxf_path)
    preview_written = True
    try:
        write_preview(model, preview_path)
    except ImportError:
        preview_written = False
    verification = write_3dm(model, model_path, verify_path)
    artifacts = {
        "json": str(json_path), "obj": str(obj_path), "mtl": str(obj_path.with_suffix('.mtl')),
        "dxf": str(dxf_path), "report": str(report_path),
    }
    if preview_written:
        artifacts["preview"] = str(preview_path)
    if verification.get("written"):
        artifacts["3dm"] = str(model_path)
        artifacts["3dm_verify"] = str(verify_path)
    write_report(model, report_path, args.source_csv, artifacts, verification)
    print(json.dumps({
        "project_name": model["project_name"],
        "object_counts": model["object_counts"],
        "bbox": model["bbox"],
        "node_corrections": model["node_corrections"],
        "modeling_adjustments": model["modeling_adjustments"],
        "fallback_sections": len(model["fallback_sections"]),
        "skipped_sections": model["skipped_sections"],
        "3dm_verification": verification,
        "outputs": artifacts,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
