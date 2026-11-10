#!/usr/bin/env python3
"""
JSON / XML 구조 파악기 — 반복을 합쳐서 "어떤 구조와 속성이 있는지"를 유니크하게 정리

- 배열 항목은 하나로 합쳐 경로로 표시 (symbols[].attributes.LineNumber)
- 경로별: 타입, 등장 횟수, 채움률(부모 대비), 고유값 개수, 자주 나오는 값, 숫자 범위
- 그룹별 속성: 객체 배열을 type/label/kind 등으로 나눠, 그룹마다 가진 키와 채움률
- XML도 입력 가능: 요소=경로, 속성=@이름, GenericAttributes는 {Name: Value}로 펼쳐서 속성 이름이 경로로 보임

사용법
  python structure.py drawing.json                       # drawing.structure.md / .structure.json
  python structure.py drawing.xml                        # AVEVA XML 구조
  python structure.py a.json b.json c.json -o all        # 여러 파일 합쳐서 하나로
  python structure.py drawing.json --group-by component_class --max-values 30
"""
import argparse
import collections
import json
import os
import sys
import xml.etree.ElementTree as ET

DISTINCT_CAP = 5000          # 경로별 고유값 추적 상한 (메모리 보호)


def local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else str(tag)


# ---------- XML -> dict ----------
def xval(v):
    """XML 속성 문자열 중 숫자는 숫자로 (범위 계산용)"""
    if v is None:
        return None
    t = v.strip()
    try:
        f = float(t)
        if t.lower() in ("nan", "inf", "-inf", "infinity", "-infinity"):
            return v
        return int(f) if f.is_integer() and "." not in t and "e" not in t.lower() else f
    except ValueError:
        return v


def xml_to_obj(el):
    """요소 -> dict. 속성은 '@이름', 같은 이름 자식은 리스트, GenericAttributes는 {Name: Value}"""
    tag = local(el.tag)
    if tag == "GenericAttributes":
        out = {f"@{local(k)}": xval(v) for k, v in el.attrib.items()}
        for ga in el:
            if local(ga.tag) == "GenericAttribute":
                name = ga.attrib.get("Name") or ga.attrib.get("name")
                if name:
                    out[name] = xval(ga.attrib.get("Value", ga.attrib.get("value")))
        return out
    out = {f"@{local(k)}": xval(v) for k, v in el.attrib.items()}
    groups = collections.OrderedDict()
    for c in el:
        groups.setdefault(local(c.tag), []).append(xml_to_obj(c))
    for k, items in groups.items():
        out[k] = items                      # 항상 리스트 -> 경로에 [] 로 표시
    text = (el.text or "").strip()
    if text:
        out["#text"] = text
    return out


def load(path):
    if path.lower().endswith(".xml"):
        root = ET.parse(path).getroot()
        return {local(root.tag): xml_to_obj(root)}
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


# ---------- 수집 ----------
class Stat:
    __slots__ = ("count", "types", "values", "capped", "nmin", "nmax", "len_min", "len_max")

    def __init__(self):
        self.count = 0
        self.types = collections.Counter()
        self.values = collections.Counter()
        self.capped = False
        self.nmin = self.nmax = None
        self.len_min = self.len_max = None


def type_name(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


class Collector:
    def __init__(self, group_keys):
        self.stats = collections.OrderedDict()
        self.group_keys = group_keys
        # (배열 경로, 그룹 키, 그룹 값) -> [개수, Counter(키)]
        self.groups = collections.OrderedDict()

    def stat(self, path):
        s = self.stats.get(path)
        if s is None:
            s = self.stats[path] = Stat()
        return s

    def walk(self, v, path="$"):
        s = self.stat(path)
        s.count += 1
        t = type_name(v)
        s.types[t] += 1
        if t == "object":
            for k, x in v.items():
                self.walk(x, f"{path}.{k}")
        elif t == "array":
            n = len(v)
            s.len_min = n if s.len_min is None else min(s.len_min, n)
            s.len_max = n if s.len_max is None else max(s.len_max, n)
            for x in v:
                if isinstance(x, dict):
                    self._group(path, x)
                self.walk(x, f"{path}[]")
        else:
            if t == "number":
                s.nmin = v if s.nmin is None else min(s.nmin, v)
                s.nmax = v if s.nmax is None else max(s.nmax, v)
            key = v if t != "string" or len(v) <= 200 else v[:200] + "…"
            if key in s.values or len(s.values) < DISTINCT_CAP:
                s.values[key] += 1
            else:
                s.capped = True

    def _group(self, arr_path, obj):
        for gk in self.group_keys:
            gv = obj.get(gk)
            if isinstance(gv, (str, int, float, bool)) and gv != "":
                rec = self.groups.setdefault((arr_path, gk, str(gv)), [0, collections.Counter()])
                rec[0] += 1
                for k, x in obj.items():
                    rec[1][k] += 1
                    if isinstance(x, dict):          # 한 단계 아래(attributes 등)까지 펼침
                        for k2 in x:
                            rec[1][f"{k}.{k2}"] += 1
                    elif isinstance(x, list):        # XML 자식 요소(GenericAttributes 등)도 한 단계 펼침
                        seen = set()
                        for item in x:
                            if isinstance(item, dict):
                                seen.update(item.keys())
                        for k2 in seen:
                            rec[1][f"{k}[].{k2}"] += 1
                return


# ---------- 출력 ----------
def parent_of(path):
    if path.endswith("[]"):
        return path[:-2]
    i = path.rfind(".")
    return path[:i] if i > 0 else None


def fmt_val(v):
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    return json.dumps(v)


def build_report(col, max_values):
    rows = []
    for path, s in col.stats.items():
        par = parent_of(path)
        pcount = col.stats[par].types.get("object", 0) if par and not path.endswith("[]") and par in col.stats else None
        distinct = len(s.values)
        top = [{"value": k, "count": c} for k, c in s.values.most_common(max_values)]
        row = {
            "path": path,
            "types": dict(s.types),
            "count": s.count,
            "fill_rate": round(s.count / pcount, 4) if pcount else None,
            "distinct": f"{distinct}+" if s.capped else distinct,
            "top_values": top,
        }
        if s.nmin is not None:
            row["min"], row["max"] = s.nmin, s.nmax
        if s.len_min is not None:
            row["array_len"] = [s.len_min, s.len_max]
        rows.append(row)
    groups = []
    order = {}
    for (arr, gk, _gv) in col.groups:
        order.setdefault((arr, gk), len(order))
    for (arr, gk, gv), (n, keys) in sorted(col.groups.items(), key=lambda kv: (order[kv[0][:2]], -kv[1][0])):
        groups.append({"array": arr, "group_key": gk, "group": gv, "count": n,
                       "keys": {k: round(c / n, 4) for k, c in sorted(keys.items(), key=lambda kv: (-kv[1], kv[0]))}})
    return {"paths": rows, "groups": groups}


def to_markdown(rep, sources, max_values):
    out = [f"# 구조 요약", "", f"입력: {', '.join(sources)}", "",
           f"고유 경로 {len(rep['paths'])}개 · 그룹 {len(rep['groups'])}개", "", "## 경로별 구조", "",
           "들여쓰기 = 계층, `[]` = 배열 항목(합쳐서 표시), 채움률 = 부모 객체 중 이 키를 가진 비율", ""]
    for r in rep["paths"]:
        path = r["path"]
        depth = path.count(".") + path.count("[]")
        name = path.rsplit(".", 1)[-1] if not path.endswith("[]") else "[]"
        types = "/".join(t for t in r["types"])
        bits = [f"×{r['count']}"]
        if r["fill_rate"] is not None and r["fill_rate"] < 1:
            bits.append(f"채움 {r['fill_rate'] * 100:.0f}%")
        if "array_len" in r:
            bits.append(f"길이 {r['array_len'][0]}~{r['array_len'][1]}")
        if r["types"].keys() - {"object", "array"}:
            bits.append(f"고유 {r['distinct']}")
            if "min" in r:
                bits.append(f"범위 {r['min']:g}~{r['max']:g}")
            if r["top_values"] and not ("number" in r["types"] and r["distinct"] != 1 and isinstance(r["distinct"], int) and r["distinct"] > max_values):
                vals = ", ".join(f"{fmt_val(t['value'])}({t['count']})" for t in r["top_values"])
                more = "" if (isinstance(r["distinct"], int) and r["distinct"] <= len(r["top_values"])) else " …"
                bits.append(f"값: {vals}{more}")
        out.append(f"{'  ' * depth}- **{name}** `{types}` " + " · ".join(bits))
    if rep["groups"]:
        out += ["", "## 그룹별 속성", "", "객체 배열을 그룹 키로 나눠, 그룹마다 가진 키와 채움률(100%면 생략)", ""]
        cur = None
        for g in rep["groups"]:
            hdr = f"### `{g['array']}` — {g['group_key']}"
            if hdr != cur:
                out += [hdr, "", "| 그룹 | 개수 | 키 |", "|---|---|---|"]
                cur = hdr
            keys = ", ".join(k if v == 1 else f"{k} ({v * 100:.0f}%)" for k, v in g["keys"].items())
            out.append(f"| {g['group']} | {g['count']} | {keys} |")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description="JSON/XML 구조를 중복 없이 정리")
    ap.add_argument("inputs", nargs="+", help=".json 또는 .xml (여러 개면 합쳐서 분석)")
    ap.add_argument("-o", "--output", help="출력 이름 (기본: 첫 입력 파일 이름)")
    ap.add_argument("--group-by", default="type,label,kind,@ComponentClass",
                    help="객체 배열을 나눌 키, 쉼표로 여러 개면 앞에서부터 있는 키 사용 (기본 type,label,kind,@ComponentClass)")
    ap.add_argument("--max-values", type=int, default=10, help="경로별로 보여줄 고유값 개수 (기본 10)")
    args = ap.parse_args()

    col = Collector([k.strip() for k in args.group_by.split(",") if k.strip()])
    for path in args.inputs:
        col.walk(load(path))
    rep = build_report(col, args.max_values)
    stem = args.output or os.path.splitext(args.inputs[0])[0]
    with open(stem + ".structure.json", "w", encoding="utf-8") as f:
        json.dump({"sources": args.inputs, **rep}, f, ensure_ascii=False, indent=2, default=str)
    with open(stem + ".structure.md", "w", encoding="utf-8") as f:
        f.write(to_markdown(rep, [os.path.basename(p) for p in args.inputs], args.max_values))
    print(f"[OK] {stem}.structure.md / .structure.json | 고유 경로 {len(rep['paths'])}개, 그룹 {len(rep['groups'])}개",
          file=sys.stderr)


if __name__ == "__main__":
    main()
