# proteus-to-json

AVEVA P&ID에서 내보낸 **Proteus XML (schema 3.3.x)** 을 JSON / CSV로 변환합니다. 표준 라이브러리만 사용합니다.

## 사용법
```bash
python proteus_to_json.py drawing.xml                 # drawing.json
python proteus_to_json.py drawing.xml -o out.json
python proteus_to_json.py *.xml --csv out_dir         # 일괄 처리 + CSV(symbols/pipes/connections)
```

## 출력
| 키 | 내용 |
|---|---|
| `symbols` | 종류(요소명 + ComponentClass), 태그, 위치, 회전각, 미러, Extent 폭·높이, GenericAttributes, ConnectionPoints(노드별 in/out), 상위 객체 |
| `pipes` | PipingNetworkSegment 속성(System 속성 상속), CenterLine 좌표, 포함 부품, 가상 세그먼트 여부 |
| `connections` | FromID/FromNode → ToID/ToNode, 양쪽 태그·종류, FlowIn/FlowOut 기반 흐름 방향, 가상 여부, 미해결 ID |
| `texts` | 도면 텍스트와 위치 |

- `PersistentID`에 `VirtualPipeSeg`가 들어간 세그먼트는 `virtual: true`로 표시됩니다.
- 예시: `examples/sample.xml` → `examples/sample.json`
