# proteus-to-json

AVEVA P&ID에서 내보낸 **Proteus XML (schema 3.3.x)** 을 JSON / CSV / 노드·엣지 그래프로 변환합니다.

- `proteus_to_json.py` — 변환 스크립트 (표준 라이브러리만 사용)
- `viewer.html` — 브라우저에서 XML을 올리면 바로 그래프로 바꿔 보여주는 뷰어 (설치 불필요, 파일은 브라우저 밖으로 나가지 않음)
- `structure.py` — JSON/XML 구조 파악기: 반복을 합쳐 경로·타입·고유값·종류별 속성을 유니크하게 정리

## 뷰어
`viewer.html`을 내려받아 브라우저로 열고 XML을 끌어다 놓으면 됩니다. 모드 두 개:
- **도면** — Neo4j 그래프를 도면 위에 겹친 것처럼: 노드는 실제 위치·크기(Extent), Tee는 원, 엣지는 노드 경계에서 끝나고 끝 화살표, 도면 텍스트는 실제 위치·크기·회전으로
- **그래프** — Neo4j 스타일 노드/엣지 (힘 기반 배치, 노드 드래그로 고정, `재배치`로 초기화). 꺾임 Tee를 끄면 꺾임을 건너뛰어 엣지 하나로 합쳐 보여줌

- 도면 좌표 그대로 표시: 노드는 Extent 크기의 사각형(크기 없으면 점선), Tee는 원 · 드래그 이동 · 휠 확대
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
- `Text` — 도면 텍스트 (내용, 위치, 높이, 회전, 정렬)
- `Tee` — 파이프끼리 만나는 지점. `tee_kinds`: `branch`(분기/파이프 간 연결), `bend`(도면상 꺾임)
- `PipeEnd` — 연결 대상이 없는 파이프 끝

**엣지**
- `PIPE` — 노드와 노드 사이의 배관 구간. 라인 속성(`attr_*`), `segment_id`, `system_id`, `flow`, `virtual`, `length`
- `PART_OF` — 노즐 → 기기
- `SIGNAL` / `LINE` — 계기 신호선 등 선 요소 (자기 Connection, 없으면 끝점 좌표로 근처 심볼에 연결)
- `LABEL_OF` — 텍스트 → 그 텍스트가 속한 심볼

변환 규칙
0. 요소 ID는 `ID` 속성, 없으면 PersistentID의 Identifier를 씁니다. Position 없이 자기 CenterLine을 가진 요소(신호선 등)는 심볼이 아니라 엣지로 봅니다. Extent가 위치와 동떨어져 있으면 심볼 로컬 좌표로 보고 위치·회전·배율·반전을 적용해 도면 좌표로 바꿉니다.
1. 세그먼트의 CenterLine들을 좌표 기준으로 이어 하나의 경로로 만듭니다. 세그먼트 밖에 있는 CenterLine은 parent id(속성 또는 GenericAttribute, ID 또는 PersistentID)로 세그먼트를 찾아 붙입니다.
2. 경로 위에 부품(밸브 등)과 분기 Tee를 위치(투영)로 끼워 넣습니다.
3. 방향이 바뀌는 꼭짓점은 꺾임 Tee 노드가 됩니다. (일직선 위의 점은 노드가 되지 않음)
4. 노드 사이 구간마다 PIPE 엣지 하나. 흐름 방향(FlowIn/FlowOut)이 반대면 엣지 방향을 뒤집습니다.
5. `VirtualPipeSeg`는 `virtual: true`인 엣지가 됩니다.
6. ComponentClass에 Tee가 들어가거나 PdmsType이 `TEE`인 실제 부품은 `Tee` 노드(`tee_source: component`)가 됩니다.
7. Connection이 없는 끝은 좌표로 근처 심볼(연결점/바운딩 박스) 또는 다른 파이프에 붙입니다. `connected_pdms_type_from/to`(TEE, NOZZ, VALV…)를 힌트로 우선 연결하며, 이렇게 붙인 엣지는 `connection_inferred`에 `start`/`end`로 표시됩니다.

노드 ID는 `도면명:원본ID` 형식이라 여러 도면을 한 그래프에 합쳐도 겹치지 않습니다.
CenterLine 좌표는 꺾임 Tee · 부품 순서 · 분기 위치를 계산하는 재료로만 쓰고 결과에는 넣지 않습니다 (필요하면 `--geometry`).
좌표 일치 허용 오차는 도면 대각선의 1/10000로 자동 결정되며(`--tol`로 지정 가능), 좌표 단위가 mm든 m든 동작합니다.

예시: `examples/sample.xml` → `sample.json`, `sample.graph.json`

## 구조 파악 (`structure.py`)
```bash
python structure.py drawing.xml                    # AVEVA XML 원본 구조
python structure.py drawing.json                   # 변환 결과 구조
python structure.py a.json b.json -o all           # 여러 파일 합쳐서
python structure.py drawing.xml --group-by @ComponentClass --max-values 30
```
`<이름>.structure.md`(읽기용)와 `.structure.json`이 생깁니다.
- 경로별: 타입, 등장 횟수, 채움률, 고유값 개수, 자주 나오는 값, 숫자 범위
- 그룹별 속성: 객체 배열을 `type`/`label`/`kind`/`@ComponentClass`로 나눠 그룹마다 가진 키(채움률)
- XML의 GenericAttributes는 `{Name: Value}`로 펼쳐 속성 이름이 경로로 보입니다
