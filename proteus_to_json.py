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
    args = ap.parse_args()

    for path in args.inputs:
        data = ProteusParser(path).run()
        stem = os.path.splitext(os.path.basename(path))[0]
        out = args.output if (args.output and len(args.inputs) == 1) else os.path.splitext(path)[0] + ".json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if args.csv:
            write_csv(data, args.csv, stem)
        s = data["summary"]
        print(f"[OK] {path} -> {out} | symbols {s['symbols']}, pipes {s['pipes']}, "
              f"connections {s['connections']} (virtual {s['virtual_connections']})", file=sys.stderr)


if __name__ == "__main__":
    main()
