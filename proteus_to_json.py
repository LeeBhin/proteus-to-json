#!/usr/bin/env python3
"""
Proteus XML (AVEVA P&ID export, schema 3.3.x) -> JSON 변환기

추출 항목
  symbols      : 심볼 위치/크기/회전/종류/태그/속성/접속점
  pipes        : 배관 세그먼트 속성(시스템 속성 상속), 중심선 좌표, 포함 부품
  connections  : 세그먼트 단위 연결관계 (FromID/FromNode -> ToID/ToNode), 흐름 방향, 가상 여부
  texts        : 도면 텍스트 (위치 포함)

사용법
  python proteus_to_json.py drawing.xml                 # drawing.json 생성
  python proteus_to_json.py drawing.xml -o out.json
  python proteus_to_json.py drawing.xml --csv out_dir   # symbols/pipes/connections CSV도 생성
  python proteus_to_json.py *.xml --csv out_dir         # 여러 파일 일괄 처리
"""
import argparse
import csv
import json
import math
import os
import sys
import xml.etree.ElementTree as ET

# 심볼로 보지 않을 요소(컨테이너/그래픽/구조 요소)
NON_SYMBOL_TAGS = {
    "PlantModel", "PlantInformation", "Drawing", "Presentation", "Extent", "Min", "Max",
    "Position", "Location", "Axis", "Reference", "Scale", "PersistentID",
    "GenericAttributes", "GenericAttribute", "ConnectionPoints", "Node",
    "Connection", "CenterLine", "Coordinate", "Line", "PolyLine", "Polyline",
    "Text", "Circle", "Ellipse", "Shape", "TrimmedCurve", "BsplineCurve",
    "PipingNetworkSystem", "PipingNetworkSegment", "ShapeCatalogue",
    "Association", "Label", "History", "UnitsOfMeasure",
}
GRAPHIC_TAGS = {"Line", "PolyLine", "Polyline", "Circle", "Ellipse", "Shape", "TrimmedCurve", "BsplineCurve"}


# ---------- 유틸 ----------
def local(tag):
    """네임스페이스 제거"""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else tag


def attr(el, name, default=None):
    """대소문자 무시 속성 조회"""
    if el is None:
        return default
    if name in el.attrib:
        return el.attrib[name]
    low = name.lower()
    for k, v in el.attrib.items():
        if local(k).lower() == low:
            return v
    return default


def child(el, name):
    if el is None:
        return None
    for c in el:
        if local(c.tag) == name:
            return c
    return None


def children(el, name):
    return [c for c in el if local(c.tag) == name] if el is not None else []


def num(v):
    if v is None:
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() and "." not in str(v) and "e" not in str(v).lower() else f
    except (TypeError, ValueError):
        return v


def xyz(el):
    if el is None:
        return None
    return {k: num(attr(el, k.upper())) for k in ("x", "y", "z")}


def parse_extent(el):
    ext = child(el, "Extent")
    if ext is None:
        return None
    mn, mx = xyz(child(ext, "Min")), xyz(child(ext, "Max"))
    if not mn or not mx or None in (mn["x"], mx["x"], mn["y"], mx["y"]):
        return {"min": mn, "max": mx}
    return {
        "min": mn,
        "max": mx,
        "width": round(mx["x"] - mn["x"], 6),
        "height": round(mx["y"] - mn["y"], 6),
        "center": {"x": round((mn["x"] + mx["x"]) / 2, 6), "y": round((mn["y"] + mx["y"]) / 2, 6)},
    }


def parse_position(el):
    pos = child(el, "Position")
    if pos is None:
        return None
    loc, axis, ref = xyz(child(pos, "Location")), xyz(child(pos, "Axis")), xyz(child(pos, "Reference"))
    out = {"location": loc, "axis": axis, "reference": ref}
    # Reference = 심볼의 로컬 X축 방향 -> 회전각
    if ref and isinstance(ref.get("x"), (int, float)) and isinstance(ref.get("y"), (int, float)):
        out["rotation_deg"] = round(math.degrees(math.atan2(ref["y"], ref["x"])) % 360, 3)
    # Axis z 가 음수면 좌우 반전(미러)
    if axis and isinstance(axis.get("z"), (int, float)):
        out["mirrored"] = axis["z"] < 0
    return out


def parse_presentation(el):
    p = child(el, "Presentation")
    if p is None:
        return None
    d = {local(k): v for k, v in p.attrib.items()}
    return d or None


def parse_generic_attributes(el):
    """GenericAttributes(여러 Set 가능) -> {name: value}"""
    result = {}
    for ga_set in children(el, "GenericAttributes"):
        set_name = attr(ga_set, "Set")
        for ga in children(ga_set, "GenericAttribute"):
            name = attr(ga, "Name")
            if not name:
                continue
            raw, fmt = attr(ga, "Value"), (attr(ga, "Format") or "").lower()
            if fmt in ("double", "float", "integer", "int", "number", "real", "decimal"):
                val = num(raw)
            elif fmt in ("boolean", "bool"):
                val = str(raw).lower() in ("true", "1", "yes")
            else:
                val = raw
            units = attr(ga, "Units")
            if units:
                val = {"value": val, "units": units}
            key = name
            if key in result and result[key] != val:   # 다른 Set에 같은 이름 -> Set 접두어
                key = f"{set_name}.{name}" if set_name else f"{name}#2"
            result[key] = val
    return result


def parse_persistent_id(el):
    pid = child(el, "PersistentID")
    if pid is None:
        return None
    return {"identifier": attr(pid, "Identifier"), "context": attr(pid, "Context")}


def parse_connection_points(el):
    cp = child(el, "ConnectionPoints")
    if cp is None:
        return None
    flow_in, flow_out = num(attr(cp, "FlowIn")), num(attr(cp, "FlowOut"))
    nodes = []
    for i, n in enumerate(children(cp, "Node")):
        loc_el = next((e for e in n.iter() if local(e.tag) == "Location"), None)
        role = None
        if flow_in is not None and i == flow_in:
            role = "in"
        if flow_out is not None and i == flow_out:
            role = "out" if role is None else "in/out"
        nodes.append({
            "index": i,          # Connection의 FromNode/ToNode가 가리키는 번호
            "id": attr(n, "ID"),
            "type": attr(n, "Type"),
            "location": xyz(loc_el),
            "flow": role,
        })
    return {
        "num_points": num(attr(cp, "NumPoints")),
        "flow_in": flow_in,
        "flow_out": flow_out,
        "nodes": nodes,
    }


def is_virtual(item):
    pid = (item.get("persistent_id") or {}).get("identifier") or ""
    return "virtual" in pid.lower() or "virtual" in str(item.get("component_class") or "").lower()


# ---------- 본체 ----------
class ProteusParser:
    def __init__(self, path):
        self.path = path
        self.root = ET.parse(path).getroot()
        self.parent = {c: p for p in self.root.iter() for c in p}
        self.symbols, self.pipes, self.connections, self.texts = [], [], [], []
        self.by_id = {}
        self.seg_el = {}      # segment id -> XML element (그래프 변환용)
        self.sys_segs = {}    # system id -> [segment id]

    def owner_id(self, el):
        p = self.parent.get(el)
        while p is not None:
            if attr(p, "ID"):
                return attr(p, "ID")
            p = self.parent.get(p)
        return None

    def ancestor(self, el, tagname):
        p = self.parent.get(el)
        while p is not None:
            if local(p.tag) == tagname:
                return p
            p = self.parent.get(p)
        return None

    def run(self):
        info = next((e for e in self.root.iter() if local(e.tag) == "PlantInformation"), None)
        drawing = next((e for e in self.root.iter() if local(e.tag) == "Drawing"), None)

        for el in self.root.iter():
            tag = local(el.tag)
            if tag == "PipingNetworkSegment":
                self._pipe(el)
            elif tag == "Connection":
                self._connection(el)
            elif tag == "Text":
                self._text(el)
            elif tag not in NON_SYMBOL_TAGS and attr(el, "ID") and (
                child(el, "Position") is not None or child(el, "Extent") is not None
                or attr(el, "ComponentClass") is not None
            ):
                self._symbol(el, tag)

        self._resolve_connections()

        return {
            "source_file": os.path.basename(self.path),
            "plant_information": {local(k): v for k, v in info.attrib.items()} if info is not None else None,
            "drawing": {
                "name": attr(drawing, "Name"),
                "type": attr(drawing, "Type"),
                "title": attr(drawing, "Title"),
                "extent": parse_extent(drawing),
                "attributes": parse_generic_attributes(drawing),
            } if drawing is not None else None,
            "summary": {
                "symbols": len(self.symbols),
                "symbols_by_type": self._count(self.symbols, "type"),
                "pipes": len(self.pipes),
                "connections": len(self.connections),
                "virtual_connections": sum(1 for c in self.connections if c["virtual"]),
                "texts": len(self.texts),
            },
            "symbols": self.symbols,
            "pipes": self.pipes,
            "connections": self.connections,
            "texts": self.texts,
        }

    @staticmethod
    def _count(items, key):
        out = {}
        for it in items:
            out[it[key]] = out.get(it[key], 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def _symbol(self, el, tag):
        item = {
            "id": attr(el, "ID"),
            "type": tag,                                   # Equipment / PipingComponent / Nozzle / ...
            "component_class": attr(el, "ComponentClass"), # 세부 종류 (GateValve 등)
            "component_name": attr(el, "ComponentName"),   # 심볼 라이브러리 이름
            "tag_name": attr(el, "TagName"),
            "parent_id": self.owner_id(el),                # 노즐 -> 기기, 부품 -> 세그먼트
            "position": parse_position(el),
            "extent": parse_extent(el),
            "scale": xyz(child(el, "Scale")),
            "presentation": parse_presentation(el),
            "persistent_id": parse_persistent_id(el),
            "attributes": parse_generic_attributes(el),
            "connection_points": parse_connection_points(el),
        }
        item["virtual"] = is_virtual(item)
        self.symbols.append(item)
        self.by_id[item["id"]] = item

    def _pipe(self, el):
        system = self.ancestor(el, "PipingNetworkSystem")
        sys_attrs = parse_generic_attributes(system) if system is not None else {}
        seg_attrs = parse_generic_attributes(el)
        centerlines = []
        for cl in el.iter():
            if local(cl.tag) == "CenterLine" and self.ancestor(cl, "PipingNetworkSegment") is el:
                centerlines.append({
                    "presentation": parse_presentation(cl),
                    "points": [xyz(c) for c in children(cl, "Coordinate")],
                })
        item = {
            "id": attr(el, "ID"),
            "system_id": attr(system, "ID") if system is not None else None,
            "component_class": attr(el, "ComponentClass"),
            "tag_name": attr(el, "TagName") or attr(system, "TagName"),
            "persistent_id": parse_persistent_id(el),
            "attributes": {**sys_attrs, **seg_attrs},   # 세그먼트 값이 시스템 값보다 우선
            "segment_attributes": seg_attrs,
            "system_attributes": sys_attrs,
            "centerlines": centerlines,
            "components": [attr(c, "ID") for c in el if attr(c, "ID") and local(c.tag) not in NON_SYMBOL_TAGS],
            "extent": parse_extent(el),
        }
        item["virtual"] = is_virtual(item)
        self.seg_el[item["id"]] = el
        self.sys_segs.setdefault(item["system_id"], []).append(item["id"])
        self.pipes.append(item)
        self.by_id[item["id"]] = item

    def _connection(self, el):
        seg = self.ancestor(el, "PipingNetworkSegment")
        self.connections.append({
            "segment_id": attr(seg, "ID") if seg is not None else self.owner_id(el),
            "from_id": attr(el, "FromID"),
            "from_node": num(attr(el, "FromNode")),
            "to_id": attr(el, "ToID"),
            "to_node": num(attr(el, "ToNode")),
        })

    def _text(self, el):
        pos = parse_position(el)
        self.texts.append({
            "string": attr(el, "String"),
            "owner_id": self.owner_id(el),
            "location": pos["location"] if pos else None,
            "height": num(attr(el, "Height")),
            "presentation": parse_presentation(el),
        })

    def _node_role(self, obj_id, node_idx):
        sym = self.by_id.get(obj_id)
        if not sym or node_idx is None:
            return None
        cp = sym.get("connection_points")
        if not cp:
            return None
        for n in cp["nodes"]:
            if n["index"] == node_idx:
                return n["flow"]
        return None

    def _resolve_connections(self):
        for c in self.connections:
            seg = self.by_id.get(c["segment_id"]) or {}
            fr, to = self.by_id.get(c["from_id"]) or {}, self.by_id.get(c["to_id"]) or {}
            c["segment_line"] = (seg.get("attributes") or {}).get("LineNumber") or seg.get("tag_name")
            c["from_type"] = fr.get("component_class") or fr.get("type")
            c["from_tag"] = fr.get("tag_name")
            c["to_type"] = to.get("component_class") or to.get("type")
            c["to_tag"] = to.get("tag_name")
            fr_role, to_role = self._node_role(c["from_id"], c["from_node"]), self._node_role(c["to_id"], c["to_node"])
            c["from_node_flow"], c["to_node_flow"] = fr_role, to_role
            # 흐름 방향: from쪽이 출구이거나 to쪽이 입구면 정방향
            if fr_role in ("out",) or to_role in ("in",):
                c["flow_direction"] = "from->to"
            elif fr_role in ("in",) or to_role in ("out",):
                c["flow_direction"] = "to->from"
            else:
                c["flow_direction"] = None
            c["virtual"] = bool(seg.get("virtual"))
            c["unresolved"] = [k for k in ("from_id", "to_id") if c[k] and c[k] not in self.by_id]


# ---------- 그래프 (노드/엣지 온톨로지) ----------
# 노드: 기기, 노즐, 배관 부품(밸브 등), 계기, Tee(꺾임 bend / 분기 branch), PipeEnd(연결 없는 끝)
# 엣지: PIPE (배관 구간), PART_OF (노즐 -> 기기)
# 세그먼트 하나를 [시작 노드, 꺾임 Tee, 부품, 분기 Tee, ..., 끝 노드] 순서로 쪼개 PIPE 엣지를 만든다.

def _flat_props(d, prefix=""):
    out = {}
    for k, v in (d or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and "value" in v:
            out[key] = v["value"]
            if v.get("units"):
                out[f"{key}_units"] = v["units"]
        elif isinstance(v, (dict, list)):
            out[key] = json.dumps(v, ensure_ascii=False)
        else:
            out[key] = v
    return out


def _xy(p):
    return (float(p["x"]), float(p["y"])) if p and p.get("x") is not None and p.get("y") is not None else None


def _dist_point_seg(p, a, b):
    ax, ay = a; bx, by = b; px, py = p
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy)


def _is_bend(prev, cur, nxt, eps=1e-3):
    v1 = (cur[0] - prev[0], cur[1] - prev[1])
    v2 = (nxt[0] - cur[0], nxt[1] - cur[1])
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 == 0 or n2 == 0:
        return False
    cross = (v1[0] * v2[1] - v1[1] * v2[0]) / (n1 * n2)
    dot = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
    return abs(cross) > eps or dot < 0


def auto_tol(p, data):
    """좌표 일치 허용 오차: 도면 크기(대각선)의 1/10000. 단위가 mm든 m든 동작하도록."""
    ext = (data.get("drawing") or {}).get("extent") or {}
    if ext.get("width") and ext.get("height"):
        diag = math.hypot(ext["width"], ext["height"])
    else:
        xs, ys = [], []
        for el in p.root.iter():
            if local(el.tag) in ("Coordinate", "Location"):
                x, y = num(attr(el, "X")), num(attr(el, "Y"))
                if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                    xs.append(x); ys.append(y)
        diag = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) if xs else 1000.0
    return diag * 1e-4 if diag > 0 else 0.1


def build_graph(p, data, tol=None, include_geometry=False):
    """CenterLine 좌표는 꺾임 Tee/부품 순서/분기 위치 계산에만 쓰고, 출력에는 넣지 않는다 (include_geometry=True면 포함)."""
    if not tol:
        tol = auto_tol(p, data)
    dname = (data.get("drawing") or {}).get("name") or os.path.splitext(data["source_file"])[0]
    nid = lambda raw: f"{dname}:{raw}"
    nodes, edges = {}, []
    sym = {s["id"]: s for s in data["symbols"]}
    seg_info = {s["id"]: s for s in data["pipes"]}
    conn_of = {}
    for c in data["connections"]:
        conn_of.setdefault(c["segment_id"], c)

    def add_node(node_id, label, props):
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "label": label, **props}
        return node_id

    # 1) 심볼 노드
    for s in data["symbols"]:
        pos = s["position"] or {}
        ext = s["extent"] or {}
        loc = _xy(pos.get("location")) or _xy(ext.get("center"))
        props = {
            "kind": s["type"],
            "component_class": s["component_class"],
            "component_name": s["component_name"],
            "tag": s["tag_name"],
            "source_id": s["id"],
            "x": loc[0] if loc else None, "y": loc[1] if loc else None,
            "rotation_deg": pos.get("rotation_deg"),
            "mirrored": pos.get("mirrored"),
            "width": ext.get("width"), "height": ext.get("height"),
            "min_x": (ext.get("min") or {}).get("x"), "min_y": (ext.get("min") or {}).get("y"),
            "max_x": (ext.get("max") or {}).get("x"), "max_y": (ext.get("max") or {}).get("y"),
            "persistent_id": (s["persistent_id"] or {}).get("identifier"),
            "drawing": dname,
        }
        props.update(_flat_props(s["attributes"], "attr_"))
        add_node(nid(s["id"]), s["component_class"] or s["type"], props)
    for s in data["symbols"]:
        if s["parent_id"] in sym:   # 노즐 -> 기기 등
            edges.append({"id": f"{nid(s['id'])}>PART_OF", "type": "PART_OF",
                          "from": nid(s["id"]), "to": nid(s["parent_id"])})

    # 2) Tee 노드 (좌표 기준 중복 제거)
    tee_at = {}

    def tee(xy, kind):
        key = (round(xy[0] / tol), round(xy[1] / tol))
        if key in tee_at:
            n = nodes[tee_at[key]]
            if kind not in n["tee_kinds"].split(";"):
                n["tee_kinds"] += ";" + kind
            return tee_at[key]
        tid = nid(f"TEE_{xy[0]:g}_{xy[1]:g}")
        tee_at[key] = tid
        add_node(tid, "Tee", {"kind": "Tee", "tee_kinds": kind, "x": xy[0], "y": xy[1], "drawing": dname})
        return tid

    def sym_xy(ref, node_idx):
        s = sym.get(ref)
        if not s:
            return None
        cp = s.get("connection_points")
        if cp and node_idx is not None:
            for n in cp["nodes"]:
                if n["index"] == node_idx and _xy(n["location"]):
                    return _xy(n["location"])
        return _xy((s["position"] or {}).get("location")) or _xy((s["extent"] or {}).get("center"))

    # 3) 세그먼트별 중심선 체인(기하 기준으로 이어 붙임) + 양끝 노드
    def chain(lines, start_hint):
        lines = [l for l in lines if l]
        if not lines:
            return []
        d = lambda a, b: math.hypot(a[0] - b[0], a[1] - b[1])
        if start_hint:
            i = min(range(len(lines)), key=lambda k: min(d(lines[k][0], start_hint), d(lines[k][-1], start_hint)))
            first = lines.pop(i)
            if d(first[-1], start_hint) < d(first[0], start_hint):
                first = first[::-1]
        else:
            first = lines.pop(0)
        out = list(first)
        while lines:
            i = min(range(len(lines)), key=lambda k: min(d(lines[k][0], out[-1]), d(lines[k][-1], out[-1])))
            nxt = lines.pop(i)
            if d(nxt[-1], out[-1]) < d(nxt[0], out[-1]):
                nxt = nxt[::-1]
            out.extend(nxt)
        return out

    segs, junctions = {}, []
    for seg_id, el in p.seg_el.items():
        lines = []
        for cl in el.iter():
            if local(cl.tag) == "CenterLine" and p.ancestor(cl, "PipingNetworkSegment") is el:
                lines.append([xy for xy in (_xy(xyz(c)) for c in children(cl, "Coordinate")) if xy])
        comps = [attr(c, "ID") for c in el.iter()
                 if attr(c, "ID") in sym and p.ancestor(c, "PipingNetworkSegment") is el
                 and sym[attr(c, "ID")]["parent_id"] == seg_id]
        conn = conn_of.get(seg_id) or {}
        s_hint = sym_xy(conn.get("from_id"), conn.get("from_node"))
        e_hint = sym_xy(conn.get("to_id"), conn.get("to_node"))
        poly = chain(lines, s_hint)
        if poly and not s_hint and e_hint:   # 끝쪽 힌트만 있으면 방향 맞추기
            if math.hypot(poly[0][0] - e_hint[0], poly[0][1] - e_hint[1]) < math.hypot(poly[-1][0] - e_hint[0], poly[-1][1] - e_hint[1]):
                poly = poly[::-1]

        def resolve(ref, node_idx, near, side):
            if ref in sym:
                return ("node", nid(ref), sym_xy(ref, node_idx) or near)
            if near is None:
                return None
            if ref in p.seg_el or ref in p.sys_segs:        # 파이프끼리 연결 / 분기
                t_id = tee(near, "branch")
                junctions.append((ref, t_id, near, seg_id))
                return ("node", t_id, near)
            e_id = nid(f"END_{seg_id}_{side}")
            add_node(e_id, "PipeEnd", {"kind": "PipeEnd", "x": near[0], "y": near[1],
                                       "unresolved_ref": ref, "drawing": dname})
            return ("node", e_id, near)

        start = resolve(conn.get("from_id"), conn.get("from_node"), poly[0] if poly else None, "start")
        end = resolve(conn.get("to_id"), conn.get("to_node"), poly[-1] if poly else None, "end")
        inserts = [(nid(c), sym_xy(c, None)) for c in comps if sym_xy(c, None)]
        segs[seg_id] = {"poly": poly, "start": start, "end": end, "inserts": inserts}

    # 4) 분기 Tee를 대상 세그먼트에 등록 (대상이 System이면 가장 가까운 세그먼트)
    def poly_dist(poly, xy):
        if len(poly) == 1:
            return math.hypot(poly[0][0] - xy[0], poly[0][1] - xy[1])
        return min((_dist_point_seg(xy, a, b) for a, b in zip(poly, poly[1:])), default=float("inf"))

    for ref, t_id, xy, src in junctions:
        cands = [ref] if ref in p.seg_el else p.sys_segs.get(ref, [])
        cands = [c for c in cands if c != src and segs.get(c, {}).get("poly")]
        if not cands:
            continue
        tgt = segs[min(cands, key=lambda c: poly_dist(segs[c]["poly"], xy))]
        for side in ("start", "end"):    # 대상의 끝점과 같은 위치면 PipeEnd를 Tee로 교체
            ent = tgt[side]
            if ent and ent[2] and math.hypot(ent[2][0] - xy[0], ent[2][1] - xy[1]) <= tol:
                if nodes.get(ent[1], {}).get("label") == "PipeEnd":
                    nodes.pop(ent[1], None)
                    tgt[side] = ("node", t_id, xy)
                break
        else:
            tgt["inserts"].append((t_id, xy))

    # 5) 경로 위 위치(호 길이)로 정렬 -> 꺾임 Tee -> PIPE 엣지
    def project(poly, xy):
        best, acc = None, 0.0
        for a, b in zip(poly, poly[1:]):
            L = math.hypot(b[0] - a[0], b[1] - a[1])
            dx, dy = b[0] - a[0], b[1] - a[1]
            t = 0.0 if L == 0 else max(0.0, min(1.0, ((xy[0] - a[0]) * dx + (xy[1] - a[1]) * dy) / (L * L)))
            dd = math.hypot(xy[0] - (a[0] + t * dx), xy[1] - (a[1] + t * dy))
            if best is None or dd < best[0]:
                best = (dd, acc + t * L)
            acc += L
        return best[1] if best else 0.0

    for seg_id, sg in segs.items():
        info = seg_info.get(seg_id, {})
        conn = conn_of.get(seg_id) or {}
        poly = sg["poly"]
        items = []          # (s, order, kind, id, xy)
        cum = [0.0]
        for a, b in zip(poly, poly[1:]):
            cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        for i, xy in enumerate(poly):
            items.append((cum[i], 1, "pt", None, xy))
        for node_id, xy in sg["inserts"]:
            items.append((project(poly, xy) if len(poly) > 1 else 0.0, 0, "node", node_id, xy))
        items.sort(key=lambda t: (t[0], t[1]))
        seq = ([list(sg["start"])] if sg["start"] else []) + [[k, i, xy] for _, _, k, i, xy in items] \
            + ([list(sg["end"])] if sg["end"] else [])
        # 노드와 같은 위치의 점은 노드에 흡수
        node_xy = [e[2] for e in seq if e[0] == "node" and e[2]]
        seq = [e for e in seq if e[0] == "node" or not any(
            math.hypot(e[2][0] - q[0], e[2][1] - q[1]) <= tol for q in node_xy)]
        # 연속 중복 제거
        clean = []
        for e in seq:
            if clean and e[0] == clean[-1][0] == "node" and e[1] == clean[-1][1]:
                continue
            if clean and e[0] == clean[-1][0] == "pt" and math.hypot(e[2][0] - clean[-1][2][0], e[2][1] - clean[-1][2][1]) <= tol:
                continue
            clean.append(e)
        for i in range(1, len(clean) - 1):
            e = clean[i]
            if e[0] == "pt":
                prv = next((x[2] for x in reversed(clean[:i]) if x[2]), None)
                nxt = next((x[2] for x in clean[i + 1:] if x[2]), None)
                if prv and nxt and _is_bend(prv, e[2], nxt):
                    e[0], e[1] = "node", tee(e[2], "bend")
        if conn.get("flow_direction") == "to->from":
            clean.reverse()
        base = {
            "segment_id": info.get("id", seg_id), "system_id": info.get("system_id"),
            "line_tag": info.get("tag_name"), "virtual": bool(info.get("virtual")),
            "flow": conn.get("flow_direction"), "drawing": dname,
        }
        base.update(_flat_props(info.get("attributes"), "attr_"))
        cur, geom, k = None, [], 0
        for e in clean:
            if e[2]:
                geom.append(e[2])
            if e[0] != "node":
                continue
            if cur is not None and cur != e[1]:
                length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(geom, geom[1:]))
                edge = {"id": f"{nid(seg_id)}#{k}", "type": "PIPE", "from": cur, "to": e[1],
                        **base, "length": round(length, 3)}
                if include_geometry:
                    edge["geometry"] = [round(v, 3) for pt in geom for v in pt]
                edges.append(edge)
                k += 1
            cur, geom = e[1], [e[2]] if e[2] else []

    return {
        "drawing": dname,
        "summary": {
            "nodes": len(nodes),
            "edges": len(edges),
            "pipe_edges": sum(1 for e in edges if e["type"] == "PIPE"),
            "tees": sum(1 for n in nodes.values() if n["label"] == "Tee"),
            "bend_tees": sum(1 for n in nodes.values() if n["label"] == "Tee" and "bend" in n["tee_kinds"]),
            "branch_tees": sum(1 for n in nodes.values() if n["label"] == "Tee" and "branch" in n["tee_kinds"]),
            "centerline_points": sum(len(sg["poly"]) for sg in segs.values()),
            "segments_without_centerline": sum(1 for sg in segs.values() if not sg["poly"]),
            "tol": tol,
            "pipe_ends": sum(1 for n in nodes.values() if n["label"] == "PipeEnd"),
        },
        "nodes": list(nodes.values()),
        "edges": edges,
    }


def write_graph_csv(g, out_dir, prefix):
    """neo4j-admin import 형식 (id:ID, :LABEL / :START_ID, :END_ID, :TYPE)"""
    os.makedirs(out_dir, exist_ok=True)
    def cell(v):
        return ";".join(map(str, v)) if isinstance(v, list) else v
    nkeys = sorted({k for n in g["nodes"] for k in n} - {"id", "label"})
    rows = [{"id:ID": n["id"], ":LABEL": n["label"], **{k: cell(n.get(k)) for k in nkeys}} for n in g["nodes"]]
    _dump(rows, os.path.join(out_dir, f"{prefix}_nodes.csv"))
    ekeys = sorted({k for e in g["edges"] for k in e} - {"from", "to", "type"})
    rows = [{":START_ID": e["from"], ":END_ID": e["to"], ":TYPE": e["type"], **{k: cell(e.get(k)) for k in ekeys}}
            for e in g["edges"]]
    _dump(rows, os.path.join(out_dir, f"{prefix}_edges.csv"))


# ---------- CSV ----------
def write_csv(data, out_dir, prefix):
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    for s in data["symbols"]:
        p, e = s["position"] or {}, s["extent"] or {}
        loc = p.get("location") or {}
        rows.append({
            "id": s["id"], "type": s["type"], "component_class": s["component_class"],
            "component_name": s["component_name"], "tag_name": s["tag_name"], "parent_id": s["parent_id"],
            "x": loc.get("x"), "y": loc.get("y"), "rotation_deg": p.get("rotation_deg"), "mirrored": p.get("mirrored"),
            "width": e.get("width"), "height": e.get("height"),
            "min_x": (e.get("min") or {}).get("x"), "min_y": (e.get("min") or {}).get("y"),
            "max_x": (e.get("max") or {}).get("x"), "max_y": (e.get("max") or {}).get("y"),
            "persistent_id": (s["persistent_id"] or {}).get("identifier"), "virtual": s["virtual"],
            "attributes": json.dumps(s["attributes"], ensure_ascii=False),
        })
    _dump(rows, os.path.join(out_dir, f"{prefix}_symbols.csv"))

    rows = [{
        "id": p["id"], "system_id": p["system_id"], "tag_name": p["tag_name"], "virtual": p["virtual"],
        "persistent_id": (p["persistent_id"] or {}).get("identifier"),
        "components": "|".join(p["components"]),
        "centerline_points": json.dumps([c["points"] for c in p["centerlines"]]),
        "attributes": json.dumps(p["attributes"], ensure_ascii=False),
    } for p in data["pipes"]]
    _dump(rows, os.path.join(out_dir, f"{prefix}_pipes.csv"))

    rows = [{k: (v if not isinstance(v, list) else "|".join(v)) for k, v in c.items()} for c in data["connections"]]
    _dump(rows, os.path.join(out_dir, f"{prefix}_connections.csv"))


def _dump(rows, path):
    if not rows:
        open(path, "w").close()
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:   # utf-8-sig: 엑셀 한글 깨짐 방지
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description="Proteus XML (AVEVA P&ID) -> JSON")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--output", help="출력 JSON 경로 (입력이 1개일 때)")
    ap.add_argument("--csv", metavar="DIR", help="CSV도 이 폴더에 생성")
    ap.add_argument("--graph", action="store_true",
                    help="노드/엣지 그래프(<이름>.graph.json) 생성. --csv와 함께 쓰면 neo4j import용 CSV도 생성")
    ap.add_argument("--tol", type=float, default=None, help="좌표 일치 허용 오차 (기본: 도면 대각선의 1/10000)")
    ap.add_argument("--geometry", action="store_true", help="그래프 엣지에 CenterLine 좌표(geometry)도 포함")
    args = ap.parse_args()

    for path in args.inputs:
        parser = ProteusParser(path)
        data = parser.run()
        stem = os.path.splitext(os.path.basename(path))[0]
        out = args.output if (args.output and len(args.inputs) == 1) else os.path.splitext(path)[0] + ".json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if args.csv:
            write_csv(data, args.csv, stem)
        if args.graph:
            g = build_graph(parser, data, args.tol, args.geometry)
            gout = os.path.splitext(out)[0] + ".graph.json"
            with open(gout, "w", encoding="utf-8") as f:
                json.dump(g, f, ensure_ascii=False, indent=2)
            if args.csv:
                write_graph_csv(g, args.csv, stem)
            gs = g["summary"]
            print(f"[GRAPH] {gout} | nodes {gs['nodes']} (tee: bend {gs['bend_tees']}, branch {gs['branch_tees']}), "
                  f"edges {gs['edges']} (pipe {gs['pipe_edges']}), centerline pts {gs['centerline_points']}, "
                  f"segments w/o centerline {gs['segments_without_centerline']}, tol {gs['tol']:.4g}", file=sys.stderr)
        s = data["summary"]
        print(f"[OK] {path} -> {out} | symbols {s['symbols']}, pipes {s['pipes']}, "
              f"connections {s['connections']} (virtual {s['virtual_connections']})", file=sys.stderr)


if __name__ == "__main__":
    main()
