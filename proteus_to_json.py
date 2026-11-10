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
import re
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
# 심볼이지만 배관을 쪼개는 노드로 만들지 않는 표시용 요소 (유동 방향 화살표, 보온 표시)
ANNOTATION_TAGS = {"PipeFlowArrow", "InsulationSymbol"}
GEOM_LIST_TAGS = {"PolyLine", "Polyline", "Line", "CenterLine", "Shape"}
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
            "nominal_diameter": (lambda nd: {"value": num(attr(nd, "Value")), "units": attr(nd, "Units")} if nd is not None else None)(child(n, "NominalDiameter")),
        })
    return {
        "num_points": num(attr(cp, "NumPoints")),
        "flow_in": flow_in,
        "flow_out": flow_out,
        "nodes": nodes,
    }


def parse_nd(el):
    """NominalDiameter: 요소 자신 또는 그 연결점/형상 안에서 첫 번째 값"""
    nd = child(el, "NominalDiameter")
    if nd is None:
        for d in el.iter():
            if local(d.tag) == "NominalDiameter":
                nd = d
                break
    if nd is None:
        return None
    return {"value": num(attr(nd, "Value")), "units": attr(nd, "Units")}


def derived_extent(el):
    """자기 Extent가 없는 심볼: 안쪽 그래픽(Extent, Coordinate, Circle/Ellipse, Text 위치)으로 바운딩 박스 계산"""
    xs, ys, used = [], [], set()

    def add(x, y, r=0.0):
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            xs.extend((x - r, x + r)); ys.extend((y - r, y + r))

    for d in el.iter():
        if d is el:
            continue
        t = local(d.tag)
        if t in ("Circle", "Ellipse"):
            loc = next((q for q in d.iter() if local(q.tag) == "Location"), None)
            r = num(attr(d, "Radius"))
            if not isinstance(r, (int, float)):
                rs = [v for v in (num(attr(d, "PrimaryRadius")), num(attr(d, "SecondaryRadius"))) if isinstance(v, (int, float))]
                r = max(rs) if rs else 0.0
            if loc is not None:
                used.add(loc)
                add(num(attr(loc, "X")), num(attr(loc, "Y")), r)
        elif t in ("Min", "Max", "Coordinate") or (t == "Location" and d not in used):
            add(num(attr(d, "X")), num(attr(d, "Y")))
    if not xs:
        return None
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    r6 = lambda v: round(v, 6)
    return {"min": {"x": r6(x0), "y": r6(y0), "z": None}, "max": {"x": r6(x1), "y": r6(y1), "z": None},
            "width": r6(x1 - x0), "height": r6(y1 - y0), "center": {"x": r6((x0 + x1) / 2), "y": r6((y0 + y1) / 2)},
            "derived": True}


def parse_cross_page(el):
    cp = child(el, "CrossPageConnection")
    if cp is None:
        return None
    return {"drawing_name": attr(cp, "DrawingName"), "link_label": attr(cp, "LinkLabel")}


def eid(el):
    """요소 ID: ID 속성, 없으면 PersistentID의 Identifier"""
    v = attr(el, "ID")
    if v:
        return v
    pid = child(el, "PersistentID")
    return attr(pid, "Identifier") if pid is not None else None


SIGNAL_RE = re.compile(r"signal|information|instrument|measur|actuat", re.I)
CENTERLINE_RE = re.compile(r"^center[ _-]?line$", re.I)
# 신호선 종류 (ComponentClass)
SIGNAL_CLASS_RE = re.compile(r"^(electric|electrical|pneumatic|hydraulic|software|data|capillary|electromagnetic|sonic|radio|"
                             r"mechanical|guided ?electromagnetic|unguided ?electromagnetic|signal.*)$", re.I)


def is_geom_carrier(el):
    """배관/신호선 형상을 담는 요소: CenterLine 요소, 또는 <Component ComponentClass="CenterLine">"""
    t = local(el.tag)
    return t == "CenterLine" or (t == "Component" and bool(CENTERLINE_RE.match(attr(el, "ComponentClass") or "")))


def point_lists(el):
    """요소 안의 PolyLine/Line/CenterLine 각각을 점 목록으로 (요소 자신의 Coordinate도 포함)"""
    out = []
    own = [q for q in (xyz(c) for c in children(el, "Coordinate")) if q]
    if own:
        out.append(own)
    for d in el.iter():
        if d is el or local(d.tag) not in GEOM_LIST_TAGS:
            continue
        pts = [q for q in (xyz(c) for c in children(d, "Coordinate")) if q]
        if pts:
            out.append(pts)
    return out


def is_line_el(el):
    """선 요소(SignalLine 등): Position 없이 자기 선 형상을 가진 요소 -> 심볼이 아니라 엣지"""
    tag = local(el.tag)
    if tag in NON_SYMBOL_TAGS or tag == "Component":
        return False
    if tag == "SignalLine" or re.search(r"(Signal|Information)(Line|Flow)$", tag):
        return True                                    # 신호선은 Position이 있어도 선
    has_geom = any(child(el, t) is not None for t in ("CenterLine", "PolyLine", "Polyline")) or any(is_geom_carrier(c) for c in el)
    if has_geom and SIGNAL_CLASS_RE.match(attr(el, "ComponentClass") or ""):
        return True                                    # ComponentClass가 신호 종류(Electric 등)인 선
    if child(el, "Position") is not None:
        return False
    if any(child(el, t) is not None for t in ("CenterLine", "Coordinate", "PolyLine", "Polyline")):
        return True
    if any(is_geom_carrier(c) for c in el):
        return True
    return bool(re.search(r"(Line|Flow)$", tag)) and child(el, "Connection") is not None


def fix_extent(ext, pos, scale):
    """Extent가 위치(Location)와 동떨어져 있으면 심볼 로컬 좌표로 보고 위치/회전/배율/반전을 적용해 도면 좌표로 변환"""
    if not ext or not pos or ext.get("width") is None:
        return ext
    loc = _xy(pos.get("location"))
    mn, mx = _xy(ext.get("min")), _xy(ext.get("max"))
    if not loc or not mn or not mx:
        return ext
    w, h = mx[0] - mn[0], mx[1] - mn[1]
    pad = max(w, h) * 0.5 + 1e-9
    if mn[0] - pad <= loc[0] <= mx[0] + pad and mn[1] - pad <= loc[1] <= mx[1] + pad:
        return ext                                    # 이미 도면 좌표
    sx = (scale or {}).get("x") if isinstance((scale or {}).get("x"), (int, float)) and (scale or {}).get("x") else 1
    sy = (scale or {}).get("y") if isinstance((scale or {}).get("y"), (int, float)) and (scale or {}).get("y") else 1
    ang = math.radians(pos.get("rotation_deg") or 0)
    ca, sa = math.cos(ang), math.sin(ang)
    pts = []
    for x, y in ((mn[0], mn[1]), (mx[0], mn[1]), (mn[0], mx[1]), (mx[0], mx[1])):
        x, y = x * sx, y * sy
        if pos.get("mirrored"):
            x = -x
        pts.append((loc[0] + x * ca - y * sa, loc[1] + x * sa + y * ca))
    x0, y0 = min(p[0] for p in pts), min(p[1] for p in pts)
    x1, y1 = max(p[0] for p in pts), max(p[1] for p in pts)
    r6 = lambda v: round(v, 6)
    return {"min": {"x": r6(x0), "y": r6(y0), "z": None}, "max": {"x": r6(x1), "y": r6(y1), "z": None},
            "width": r6(x1 - x0), "height": r6(y1 - y0), "center": {"x": r6((x0 + x1) / 2), "y": r6((y0 + y1) / 2)},
            "local": True, "local_min": ext.get("min"), "local_max": ext.get("max")}


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
        self.lines, self.line_by_el = [], {}
        self.sys_segs = {}    # system id -> [segment id]

    def owner_id(self, el):
        p = self.parent.get(el)
        while p is not None:
            if eid(p) and local(p.tag) not in ("PersistentID",):
                return eid(p)
            p = self.parent.get(p)
        return None

    def in_geom(self, el):
        p = self.parent.get(el)
        while p is not None:
            if is_geom_carrier(p) or local(p.tag) == "Component":
                return True
            p = self.parent.get(p)
        return False

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
            elif is_line_el(el) and eid(el):
                self._line(el, tag)
            elif tag == "Component" or is_geom_carrier(el) or self.in_geom(el):
                continue                                   # 형상(중심선 등) - 심볼 아님
            elif tag not in NON_SYMBOL_TAGS and eid(el) and (
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
                "lines": len(self.lines),
                "texts": len(self.texts),
            },
            "symbols": self.symbols,
            "pipes": self.pipes,
            "connections": self.connections,
            "lines": self.lines,
            "texts": self.texts,
        }

    @staticmethod
    def _count(items, key):
        out = {}
        for it in items:
            out[it[key]] = out.get(it[key], 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def _symbol(self, el, tag):
        position, scale = parse_position(el), xyz(child(el, "Scale"))
        item = {
            "id": eid(el),
            "type": tag,                                   # Equipment / PipingComponent / Nozzle / ...
            "component_class": attr(el, "ComponentClass"), # 세부 종류 (GateValve 등)
            "component_name": attr(el, "ComponentName"),   # 심볼 라이브러리 이름
            "tag_name": attr(el, "TagName"),
            "parent_id": self.owner_id(el),                # 노즐 -> 기기, 부품 -> 세그먼트
            "position": position,
            "extent": fix_extent(parse_extent(el), position, scale) or derived_extent(el),
            "scale": scale,
            "presentation": parse_presentation(el),
            "persistent_id": parse_persistent_id(el),
            "attributes": parse_generic_attributes(el),
            "connection_points": parse_connection_points(el),
            "nominal_diameter": parse_nd(el),
            "cross_page": parse_cross_page(el),
            "annotation": tag in ANNOTATION_TAGS,
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
            if is_geom_carrier(cl) and self.ancestor(cl, "PipingNetworkSegment") is el:
                for pts in point_lists(cl):
                    centerlines.append({"presentation": parse_presentation(cl), "points": pts})
        item = {
            "id": eid(el),
            "system_id": eid(system) if system is not None else None,
            "component_class": attr(el, "ComponentClass"),
            "tag_name": attr(el, "TagName") or attr(system, "TagName"),
            "persistent_id": parse_persistent_id(el),
            "attributes": {**sys_attrs, **seg_attrs},   # 세그먼트 값이 시스템 값보다 우선
            "segment_attributes": seg_attrs,
            "system_attributes": sys_attrs,
            "centerlines": centerlines,
            "components": [eid(c) for c in el if eid(c) and local(c.tag) not in NON_SYMBOL_TAGS and local(c.tag) != "Component"],
            "nominal_diameter": next((parse_nd(c) for c in el if is_geom_carrier(c) and parse_nd(c)), None),
            "extent": parse_extent(el),
        }
        item["virtual"] = is_virtual(item)
        self.seg_el[item["id"]] = el
        self.sys_segs.setdefault(item["system_id"], []).append(item["id"])
        self.pipes.append(item)
        self.by_id[item["id"]] = item

    def _line(self, el, tag):
        pts = [q for lst in point_lists(el) for q in lst]
        item = {
            "id": eid(el), "type": tag,
            "kind": "SIGNAL" if SIGNAL_RE.search(tag) or SIGNAL_CLASS_RE.match(attr(el, "ComponentClass") or "") else "LINE",
            "component_class": attr(el, "ComponentClass"), "tag_name": attr(el, "TagName"),
            "persistent_id": parse_persistent_id(el), "attributes": parse_generic_attributes(el),
            "points": pts, "from_id": None, "from_node": None, "to_id": None, "to_node": None,
        }
        self.lines.append(item)
        self.line_by_el[el] = item

    def _connection(self, el):
        owner = self.parent.get(el)
        if owner in self.line_by_el:                 # 선 요소(신호선 등)의 연결
            ln = self.line_by_el[owner]
            if ln["from_id"] is None and ln["to_id"] is None:
                ln.update(from_id=attr(el, "FromID"), from_node=num(attr(el, "FromNode")),
                          to_id=attr(el, "ToID"), to_node=num(attr(el, "ToNode")))
            return
        seg = self.ancestor(el, "PipingNetworkSegment")
        self.connections.append({
            "segment_id": eid(seg) if seg is not None else self.owner_id(el),
            "from_id": attr(el, "FromID"),
            "from_node": num(attr(el, "FromNode")),
            "to_id": attr(el, "ToID"),
            "to_node": num(attr(el, "ToNode")),
        })

    def _text(self, el):
        pos = parse_position(el)
        sym_id = None                                  # 텍스트가 속한 가장 가까운 심볼
        p = self.parent.get(el)
        while p is not None:
            pid = eid(p) if local(p.tag) != "PersistentID" else None
            if pid and pid in self.by_id and "type" in self.by_id[pid] and pid not in self.seg_el:
                sym_id = pid
                break
            p = self.parent.get(p)
        self.texts.append({
            "string": attr(el, "String"),
            "owner_id": self.owner_id(el),
            "symbol_id": sym_id,
            "location": pos["location"] if pos else None,
            "rotation_deg": num(attr(el, "TextAngle")) if num(attr(el, "TextAngle")) is not None else (pos.get("rotation_deg") if pos else None),
            "height": num(attr(el, "Height")),
            "width": num(attr(el, "Width")),
            "justification": attr(el, "Justification"),
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
    sym = {s["id"]: s for s in data["symbols"] if not s.get("annotation")}   # 유동 화살표/보온 표시는 노드 아님
    seg_info = {s["id"]: s for s in data["pipes"]}
    conn_of = {}
    for c in data["connections"]:
        conn_of.setdefault(c["segment_id"], c)

    def add_node(node_id, label, props):
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "label": label, **props}
        return node_id

    # 0) 실제 Tee 부품 (ComponentClass에 tee 포함, 또는 PdmsType = TEE)
    def is_tee_sym(s):
        if re.search(r"tee", s.get("component_class") or "", re.I):
            return True
        return any(re.fullmatch(r"pdms[ _-]?type", k, re.I) and str(v).strip().upper() == "TEE"
                   for k, v in (s.get("attributes") or {}).items())

    # 1) 심볼 노드
    for s in sym.values():
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
        if is_tee_sym(s):     # 실제 Tee 부품도 온톨로지의 Tee 노드
            props.update({"tee_kinds": "branch", "tee_source": "component"})
        nd = s.get("nominal_diameter") or {}
        if nd.get("value") is not None:
            props.update({"nominal_diameter": nd["value"], "nominal_diameter_units": nd.get("units")})
        cp = s.get("cross_page") or {}
        if cp:
            cc = (s.get("component_class") or "").lower()
            props.update({"cross_drawing": cp.get("drawing_name"), "cross_link_label": cp.get("link_label"),
                          "connector_direction": "in" if "inbound" in cc else "out" if "outbound" in cc else None})
        props.update(_flat_props(s["attributes"], "attr_"))
        add_node(nid(s["id"]), "Tee" if is_tee_sym(s) else (s["component_class"] or s["type"]), props)
    for s in sym.values():
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
        add_node(tid, "Tee", {"kind": "Tee", "tee_kinds": kind, "tee_source": "geometry" if kind == "bend" else "connection",
                              "x": xy[0], "y": xy[1], "drawing": dname})
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

    def poly_dist(poly, xy):
        if len(poly) == 1:
            return math.hypot(poly[0][0] - xy[0], poly[0][1] - xy[1])
        return min((_dist_point_seg(xy, a, b) for a, b in zip(poly, poly[1:])), default=float("inf"))

    def closest_on_poly(poly, xy):
        best = None
        for a, b in zip(poly, poly[1:]):
            dx, dy = b[0] - a[0], b[1] - a[1]
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((xy[0] - a[0]) * dx + (xy[1] - a[1]) * dy) / L2))
            q = (a[0] + t * dx, a[1] + t * dy)
            dd = math.hypot(xy[0] - q[0], xy[1] - q[1])
            if best is None or dd < best[0]:
                best = (dd, q)
        return best[1] if best else poly[0]

    # 3-1) 세그먼트별 CenterLine 모으기: 세그먼트 안에 있는 것 + parent id로 세그먼트를 가리키는 바깥 것
    pid2seg = {(i.get("persistent_id") or {}).get("identifier"): sid for sid, i in seg_info.items()
               if (i.get("persistent_id") or {}).get("identifier")}
    pid2sym = {(s["persistent_id"] or {}).get("identifier"): s["id"] for s in data["symbols"]
               if (s["persistent_id"] or {}).get("identifier")}

    def parent_ref(cl):
        for name in ("ParentID", "Parent", "ParentRef", "OwnerID", "Owner"):
            v = attr(cl, name)
            if v:
                return v
        for k, v in parse_generic_attributes(cl).items():
            if re.fullmatch(r"(parent|owner)[ _-]?(id|ref)?", k, re.I) and v not in (None, ""):
                return str(v["value"] if isinstance(v, dict) else v)
        return None

    seg_lines = {sid: [] for sid in p.seg_el}
    sys_lines, unassigned_lines, outside_lines = {}, 0, 0

    def in_line_el(el):
        q = p.parent.get(el)
        while q is not None:
            if q in p.line_by_el:
                return True
            q = p.parent.get(q)
        return False

    def nested_carrier(el):
        q = p.parent.get(el)
        while q is not None:
            if is_geom_carrier(q):
                return True
            q = p.parent.get(q)
        return False

    carriers = []
    for el in p.root.iter():
        if is_geom_carrier(el) and not nested_carrier(el):
            carriers.append(el)
        elif local(el.tag) in ("PolyLine", "Polyline") and local(p.parent.get(el).tag) == "PipingNetworkSegment":
            carriers.append(el)                       # 세그먼트 바로 아래 PolyLine
    for cl in carriers:
        if p.parent.get(cl) in p.line_by_el or in_line_el(cl):   # 신호선 등 선 요소의 형상은 제외
            continue
        lists = point_lists(cl) if is_geom_carrier(cl) else [[q for q in (xyz(c) for c in children(cl, "Coordinate")) if q]]
        lists = [[xy for xy in (_xy(q) for q in lst) if xy] for lst in lists]
        lists = [l for l in lists if l]
        if not lists:
            continue
        owner = p.ancestor(cl, "PipingNetworkSegment")
        if owner is not None and eid(owner) in seg_lines:
            seg_lines[eid(owner)].extend(lists)
            continue
        outside_lines += 1
        ref = parent_ref(cl)
        if ref in seg_lines:
            seg_lines[ref].extend(lists)
        elif ref in pid2seg:
            seg_lines[pid2seg[ref]].extend(lists)
        else:
            sys_el = p.ancestor(cl, "PipingNetworkSystem")
            sref = ref if ref in p.sys_segs else (eid(sys_el) if sys_el is not None else None)
            if sref in p.sys_segs:
                sys_lines.setdefault(sref, []).extend(lists)
            else:
                unassigned_lines += 1

    def seg_comps(seg_id, el):
        return [eid(c) for c in el.iter()
                if local(c.tag) != "PersistentID" and eid(c) in sym and p.ancestor(c, "PipingNetworkSegment") is el
                and sym[eid(c)]["parent_id"] == seg_id]

    # System만 가리키는 CenterLine은 부품/연결 위치가 가장 가까운 세그먼트에 배정
    for sref, lines in sys_lines.items():
        cands = p.sys_segs.get(sref, [])
        for pts in lines:
            def score(sid):
                conn = conn_of.get(sid) or {}
                anchors = [sym_xy(c, None) for c in seg_comps(sid, p.seg_el[sid])]
                anchors += [sym_xy(conn.get("from_id"), conn.get("from_node")), sym_xy(conn.get("to_id"), conn.get("to_node"))]
                anchors += [q for l in seg_lines[sid] for q in l]
                anchors = [a for a in anchors if a]
                return min((math.hypot(a[0] - q[0], a[1] - q[1]) for a in anchors for q in (pts[0], pts[-1])), default=float("inf"))
            if cands:
                seg_lines[min(cands, key=score)].append(pts)

    # 3-2) 세그먼트 경로
    def attr_handle(info, side):
        for k, v in (info.get("attributes") or {}).items():
            if re.fullmatch(rf"connected[ _-]?handle[ _-]?{side}", k, re.I) and v not in (None, ""):
                return str(v["value"] if isinstance(v, dict) else v).strip()
        return None

    def attr_hint(info, side):
        for k, v in (info.get("attributes") or {}).items():
            if re.fullmatch(rf"connected[ _-]?pdms[ _-]?type[ _-]?{side}", k, re.I) and v:
                return str(v["value"] if isinstance(v, dict) else v).strip().upper()
        return None

    segs = {}
    for seg_id, el in p.seg_el.items():
        comps = seg_comps(seg_id, el)
        conn = dict(conn_of.get(seg_id) or {})
        info_ = seg_info.get(seg_id, {})
        if not conn.get("from_id") and attr_handle(info_, "from"):   # Connection이 없으면 connected_handle_from/to 사용
            conn["from_id"], conn["from_node"] = attr_handle(info_, "from"), None
        if not conn.get("to_id") and attr_handle(info_, "to"):
            conn["to_id"], conn["to_node"] = attr_handle(info_, "to"), None
        s_hint = sym_xy(pid2sym.get(conn.get("from_id"), conn.get("from_id")), conn.get("from_node"))
        e_hint = sym_xy(pid2sym.get(conn.get("to_id"), conn.get("to_id")), conn.get("to_node"))
        poly = chain(seg_lines.get(seg_id, []), s_hint)
        if poly and not s_hint and e_hint:   # 끝쪽 힌트만 있으면 방향 맞추기
            if math.hypot(poly[0][0] - e_hint[0], poly[0][1] - e_hint[1]) < math.hypot(poly[-1][0] - e_hint[0], poly[-1][1] - e_hint[1]):
                poly = poly[::-1]
        segs[seg_id] = {"poly": poly, "comps": comps, "conn": conn, "inferred": [],
                        "hint_from": attr_hint(seg_info.get(seg_id, {}), "from"),
                        "hint_to": attr_hint(seg_info.get(seg_id, {}), "to")}

    # 3-3) 양끝 연결: Connection -> (없으면) 좌표로 근처 심볼/파이프에 붙임
    snap = tol * 30
    junctions = []

    def sym_dist(s, xy):
        ds = []
        cp = s.get("connection_points")
        if cp:
            ds += [math.hypot(xy[0] - q[0], xy[1] - q[1]) for q in (_xy(n["location"]) for n in cp["nodes"]) if q]
        loc = _xy((s["position"] or {}).get("location"))
        if loc:
            ds.append(math.hypot(xy[0] - loc[0], xy[1] - loc[1]))
        ext = s["extent"] or {}
        mn, mx = _xy(ext.get("min")), _xy(ext.get("max"))
        if mn and mx:
            dx = max(mn[0] - xy[0], 0, xy[0] - mx[0])
            dy = max(mn[1] - xy[1], 0, xy[1] - mx[1])
            ds.append(math.hypot(dx, dy))
        return min(ds) if ds else float("inf")

    def hint_ok(s, hint):
        if not hint:
            return True
        if hint == "TEE":
            return is_tee_sym(s)
        if hint.startswith("NOZZ"):
            return s["type"] == "Nozzle"
        if hint.startswith("VALV"):
            return bool(re.search(r"valve", s.get("component_class") or "", re.I))
        return True

    def snap_sym(xy, hint, exclude):
        best = None
        for s in sym.values():
            if s["id"] in exclude:
                continue
            d = sym_dist(s, xy)
            if d > snap:
                continue
            ext = s["extent"] or {}
            area = (ext.get("width") or 0) * (ext.get("height") or 0)
            key = (0 if hint_ok(s, hint) else 1, d, area)
            if best is None or key < best[0]:
                best = (key, s["id"])
        return best[1] if best else None

    def resolve(seg_id, ref, node_idx, near, side, hint):
        sg = segs[seg_id]
        if ref and ref not in sym and ref in pid2sym:
            ref = pid2sym[ref]
        if ref in sym:
            return ("node", nid(ref), sym_xy(ref, node_idx) or near)
        if near is None:
            return None
        if ref in p.seg_el or ref in p.sys_segs or ref in pid2seg:   # 파이프끼리 연결 / 분기
            hit = snap_sym(near, "TEE", set(sg["comps"]))
            if hit and is_tee_sym(sym[hit]):
                return ("node", nid(hit), sym_xy(hit, None) or near)
            t_id = tee(near, "branch")
            junctions.append((pid2seg.get(ref, ref), t_id, near, seg_id))
            return ("node", t_id, near)
        # Connection 없음 -> 좌표로 추정
        hit = snap_sym(near, hint, set(sg["comps"]))
        if hit:
            sg["inferred"].append(side)
            return ("node", nid(hit), sym_xy(hit, None) or near)
        best = None
        for sid, other in segs.items():
            if sid == seg_id or len(other["poly"]) < 2:
                continue
            d = poly_dist(other["poly"], near)
            if d <= snap and (best is None or d < best[0]):
                best = (d, sid)
        if best:
            q = closest_on_poly(segs[best[1]]["poly"], near)
            t_id = tee(q, "branch")
            junctions.append((best[1], t_id, q, seg_id))
            sg["inferred"].append(side)
            return ("node", t_id, q)
        e_id = nid(f"END_{seg_id}_{side}")
        add_node(e_id, "PipeEnd", {"kind": "PipeEnd", "x": near[0], "y": near[1], "unresolved_ref": ref,
                                   "hint": hint, "drawing": dname})
        return ("node", e_id, near)

    for seg_id, sg in segs.items():
        conn, poly = sg["conn"], sg["poly"]
        sg["start"] = resolve(seg_id, conn.get("from_id"), conn.get("from_node"), poly[0] if poly else None, "start", sg["hint_from"])
        sg["end"] = resolve(seg_id, conn.get("to_id"), conn.get("to_node"), poly[-1] if poly else None, "end", sg["hint_to"])
        sg["inserts"] = [(nid(c), sym_xy(c, None)) for c in sg["comps"] if sym_xy(c, None)]

    # 4) 분기 Tee를 대상 세그먼트에 등록 (대상이 System이면 가장 가까운 세그먼트)
    for ref, t_id, xy, src in junctions:
        cands = [ref] if ref in p.seg_el else p.sys_segs.get(ref, [])
        cands = [c for c in cands if c != src and segs.get(c, {}).get("poly")]
        if not cands:
            continue
        tgt = segs[min(cands, key=lambda c: poly_dist(segs[c]["poly"], xy))]
        if t_id in [e[1] for e in (tgt["start"], tgt["end"]) if e] or t_id in [i[0] for i in tgt["inserts"]]:
            continue
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
        flow, flow_source = conn.get("flow_direction"), "connection" if conn.get("flow_direction") else None
        if not flow and len(poly) > 1:
            # 유동 방향 화살표(PipeFlowArrow): 화살표의 Reference 방향과 경로 방향 비교
            for a in data["symbols"]:
                if a["type"] != "PipeFlowArrow" or a["parent_id"] != seg_id:
                    continue
                ref = _xy((a["position"] or {}).get("reference"))
                loc = _xy((a["position"] or {}).get("location"))
                if not ref or not loc:
                    continue
                best = None
                for u, w in zip(poly, poly[1:]):
                    dd = _dist_point_seg(loc, u, w)
                    if best is None or dd < best[0]:
                        best = (dd, (w[0] - u[0], w[1] - u[1]))
                if best:
                    dot = ref[0] * best[1][0] + ref[1] * best[1][1]
                    if dot:
                        flow, flow_source = ("from->to" if dot > 0 else "to->from"), "arrow"
                        break
        if flow == "to->from":
            clean.reverse()
        base = {
            "segment_id": info.get("id", seg_id), "system_id": info.get("system_id"),
            "line_tag": info.get("tag_name"), "virtual": bool(info.get("virtual")),
            "flow": flow, "flow_source": flow_source, "drawing": dname,
            "connection_inferred": ";".join(sg["inferred"]) or None,
            "nominal_diameter": (info.get("nominal_diameter") or {}).get("value"),
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

    # 6) 텍스트 -> Text 노드 (+ 심볼에 속하면 LABEL_OF 엣지)
    for i, t in enumerate(data.get("texts", [])):
        if not t.get("string"):
            continue
        loc = _xy(t.get("location"))
        t_id = nid(f"TEXT_{i}")
        add_node(t_id, "Text", {"kind": "Text", "text": t["string"], "x": loc[0] if loc else None, "y": loc[1] if loc else None,
                                "height": t.get("height"), "rotation_deg": t.get("rotation_deg"),
                                "justification": t.get("justification"), "owner_id": t.get("owner_id"), "drawing": dname})
        if t.get("symbol_id") in sym:
            edges.append({"id": f"{t_id}>LABEL_OF", "type": "LABEL_OF", "from": t_id, "to": nid(t["symbol_id"])})

    # 7) 선 요소(계기 신호선 등) -> SIGNAL / LINE 엣지
    lines_unconnected = 0
    for ln in data.get("lines", []):
        pts = [q for q in (_xy(pt) for pt in ln["points"]) if q]
        ends = []
        for ref, idx, near in ((ln["from_id"], ln["from_node"], pts[0] if pts else None),
                               (ln["to_id"], ln["to_node"], pts[-1] if pts else None)):
            if ref and ref not in sym and ref in pid2sym:
                ref = pid2sym[ref]
            if ref in sym:
                ends.append((nid(ref), False))
            elif near is not None and snap_sym(near, None, set()):
                ends.append((nid(snap_sym(near, None, set())), True))
            else:
                ends.append(None)
        if not ends[0] or not ends[1] or ends[0][0] == ends[1][0]:
            lines_unconnected += 1
            continue
        e = {"id": nid(ln["id"]), "type": ln["kind"], "from": ends[0][0], "to": ends[1][0],
             "line_type": ln["type"], "signal_type": ln.get("component_class"), "tag": ln["tag_name"], "drawing": dname,
             "connection_inferred": ";".join(s for s, (_, inf) in zip(("start", "end"), ends) if inf) or None}
        e.update(_flat_props(ln["attributes"], "attr_"))
        edges.append(e)

    # 8) 위치를 못 구한 노드: 연결된 이웃 노드들의 평균 위치 (position_inferred)
    no_pos = [n for n in nodes.values() if n.get("x") is None]
    adj = {}
    for e in edges:
        adj.setdefault(e["from"], []).append(e["to"])
        adj.setdefault(e["to"], []).append(e["from"])
    for _ in range(5):
        changed = False
        for n in no_pos:
            if n.get("x") is not None:
                continue
            pts = [(nodes[m]["x"], nodes[m]["y"]) for m in adj.get(n["id"], []) if m in nodes and nodes[m].get("x") is not None]
            if pts:
                n["x"] = round(sum(q[0] for q in pts) / len(pts), 6)
                n["y"] = round(sum(q[1] for q in pts) / len(pts), 6)
                n["position_inferred"] = True
                changed = True
        if not changed:
            break

    return {
        "drawing": dname,
        "summary": {
            "nodes": len(nodes),
            "nodes_without_position": len(no_pos),
            "nodes_position_inferred": sum(1 for n in no_pos if n.get("position_inferred")),
            "edges": len(edges),
            "pipe_edges": sum(1 for e in edges if e["type"] == "PIPE"),
            "tees": sum(1 for n in nodes.values() if n["label"] == "Tee"),
            "bend_tees": sum(1 for n in nodes.values() if n["label"] == "Tee" and "bend" in n["tee_kinds"]),
            "branch_tees": sum(1 for n in nodes.values() if n["label"] == "Tee" and "branch" in n["tee_kinds"]),
            "centerline_points": sum(len(sg["poly"]) for sg in segs.values()),
            "centerlines_outside_segment": outside_lines,
            "centerlines_unassigned": unassigned_lines,
            "component_tees": sum(1 for n in nodes.values() if n.get("tee_source") == "component"),
            "inferred_connection_ends": sum(len(sg["inferred"]) for sg in segs.values()),
            "line_edges": sum(1 for e in edges if e["type"] in ("SIGNAL", "LINE")),
            "texts": sum(1 for n in nodes.values() if n["label"] == "Text"),
            "lines_unconnected": lines_unconnected,
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
                  f"segments w/o centerline {gs['segments_without_centerline']}, outside centerlines {gs['centerlines_outside_segment']} "
                  f"(unassigned {gs['centerlines_unassigned']}), component tees {gs['component_tees']}, "
                  f"inferred ends {gs['inferred_connection_ends']}, tol {gs['tol']:.4g}", file=sys.stderr)
        s = data["summary"]
        print(f"[OK] {path} -> {out} | symbols {s['symbols']}, pipes {s['pipes']}, "
              f"connections {s['connections']} (virtual {s['virtual_connections']})", file=sys.stderr)


if __name__ == "__main__":
    main()
