# proteus-to-json

AVEVA P&ID에서 내보낸 **Proteus XML (schema 3.3.x)** 을 JSON / CSV / 노드·엣지 그래프로 변환합니다.

- `proteus_to_json.py` — 변환 스크립트 (표준 라이브러리만 사용)
- `viewer.html` — 브라우저에서 XML을 올리면 바로 그래프로 바꿔 보여주는 뷰어 (설치 불필요, 파일은 브라우저 밖으로 나가지 않음)

## 뷰어
`viewer.html`을 내려받아 브라우저로 열고 XML을 끌어다 놓으면 됩니다.
- 도면 좌표 그대로 노드/파이프 표시, 드래그 이동 · 휠 확대
- 노드 클릭: 속성 + 연결된 파이프, 파이프 클릭: 같은 라인 강조
- 태그 / 라인번호 검색
- `graph.json`, `nodes.csv`, `edges.csv` 내려받기 (neo4j-admin import 헤더 형식)

## 스크립트
```bash
python proteus_to_json.py drawing.xml                 # drawing.json
python proteus_to_json.py drawing.xml --graph         # + drawing.graph.json (노드/엣지)
python proteus_to_json.py *.xml --graph --csv out     # 일괄 처리 + CSV (원본 표 + neo4j용 nodes/edges)
```
Windows에서 `python`이 안 되면 `py`를 쓰세요.

## 출력 1: 원본 구조 (`drawing.json`)
| 키 | 내용 |
|---|---|
| `symbols` | 종류(요소명 + ComponentClass), 태그, 위치, 회전각, 미러, Extent 폭·높이, GenericAttributes, ConnectionPoints(노드별 in/out), 상위 객체 |
| `pipes` | PipingNetworkSegment(라인의 한 구간) 속성(System 속성 상속), CenterLine 좌표, 포함 부품, 가상 여부 |
| `connections` | FromID/FromNode → ToID/ToNode, FlowIn/FlowOut 기반 흐름 방향, 가상 여부 |
| `texts` | 도면 텍스트와 위치 |

## 출력 2: 그래프 (`drawing.graph.json`)
노드와 엣지만으로 표현합니다. **파이프는 엣지**입니다.

**노드**
- 기기(Equipment), 노즐(Nozzle), 배관 부품(밸브 등), 계기
- `Tee` — 파이프끼리 만나는 지점. `tee_kinds`: `branch`(분기/파이프 간 연결), `bend`(도면상 꺾임)
- `PipeEnd` — 연결 대상이 없는 파이프 끝

**엣지**
- `PIPE` — 노드와 노드 사이의 배관 구간. 라인 속성(`attr_*`), `segment_id`, `system_id`, `flow`, `virtual`, `length`, `geometry`(x1,y1,x2,y2,...)
- `PART_OF` — 노즐 → 기기

변환 규칙
1. 세그먼트의 CenterLine들을 좌표 기준으로 이어 하나의 경로로 만듭니다.
2. 경로 위에 부품(밸브 등)과 분기 Tee를 위치(투영)로 끼워 넣습니다.
3. 방향이 바뀌는 꼭짓점은 꺾임 Tee 노드가 됩니다. (일직선 위의 점은 노드가 되지 않음)
4. 노드 사이 구간마다 PIPE 엣지 하나. 흐름 방향(FlowIn/FlowOut)이 반대면 엣지 방향을 뒤집습니다.
5. `VirtualPipeSeg`는 `virtual: true`인 엣지가 됩니다.

노드 ID는 `도면명:원본ID` 형식이라 여러 도면을 한 그래프에 합쳐도 겹치지 않습니다.
좌표 일치 허용 오차는 `--tol` (기본 0.5).

예시: `examples/sample.xml` → `sample.json`, `sample.graph.json`
