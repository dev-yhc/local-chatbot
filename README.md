# Local Chatbot

Ollama 위에 얹은 가벼운 FastAPI 챗봇 서버. 팀 내부용으로 시작해서 나중에 웹서치/MCP 같은 도구를 붙이기 좋은 베이스로 짰음.

## 구조

- `main.py` — FastAPI 앱. Ollama의 OpenAI 호환 엔드포인트(`/v1`)를 향해 OpenAI SDK로 호출함
- `docker-compose.yml` — Open WebUI 컨테이너 (UI + 멀티유저)
- `requirements.txt` — 의존성
- `.env.example` — 환경변수 예시

엔드포인트:

| Method | Path                    | 설명                                                       |
|--------|-------------------------|------------------------------------------------------------|
| GET    | `/health`               | 서버 상태와 현재 모델 확인                                 |
| GET    | `/models`               | Ollama에 받아둔 모델 목록                                  |
| POST   | `/chat`                 | 챗 완료 (커스텀 포맷). `stream: true` 주면 SSE로 스트리밍   |
| GET    | `/v1/models`            | OpenAI 호환 모델 목록 (Open WebUI / OpenAI SDK용)          |
| POST   | `/v1/chat/completions`  | OpenAI 호환 챗 완료 (스트리밍 포함). 도구/MCP 그대로 적용  |

## 1. Ollama 설치

### macOS

```bash
brew install ollama
# 또는 https://ollama.com/download 에서 .dmg 다운로드
```

설치하면 백그라운드 서비스로 떠 있음 (`http://localhost:11434`). 안 떠 있으면:

```bash
ollama serve
```

### Linux (서버 이식할 때)

```bash
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl enable --now ollama
```

기본적으로 `127.0.0.1:11434`에만 바인딩됨. 다른 머신에서 붙어야 하면 `OLLAMA_HOST=0.0.0.0:11434` 환경변수 설정 후 재시작.

## 2. 모델 받기

```bash
# 권장 (도구 호출 잘 됨, 7B면 M1 Pro 16GB에서도 무난)
ollama pull qwen2.5:7b

# 더 작게 시작하고 싶으면
ollama pull qwen2.5:3b

# 더 큰 거 돌릴 수 있으면 (32GB+ 권장)
ollama pull qwen2.5:14b

# 다른 후보
ollama pull llama3.1:8b
```

설치된 모델 확인:

```bash
ollama list
```

CLI에서 바로 대화해보기 (서버 안 띄워도 됨):

```bash
ollama run qwen2.5:7b
```

## 3. 챗봇 서버 띄우기

가상환경 만들고 의존성 설치:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

환경변수 (필요할 때만 — 기본값으로도 동작함):

```bash
cp .env.example .env
# 모델 바꾸고 싶으면 .env에서 OLLAMA_MODEL 수정
```

실행:

```bash
uvicorn main:app --reload --port 8000
```

`http://localhost:8000/docs` 에서 Swagger UI로 바로 테스트 가능.

## 4. 호출 예시

```bash
# 헬스체크
curl http://localhost:8000/health

# 일반 응답
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "system", "content": "한국어로 짧게 답해."},
      {"role": "user", "content": "FastAPI 장점 3가지만."}
    ]
  }'

# 스트리밍 (SSE)
curl -N -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "안녕"}],
    "stream": true
  }'

# 다른 모델 즉석 지정
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3.1:8b",
    "messages": [{"role": "user", "content": "hi"}]
  }'
```

## 5. 서버 이식

맥북에서 잘 돌면 그대로 Linux 서버에 옮길 수 있음:

1. Linux 서버에 Ollama 설치 + 같은 모델 `pull`
2. 이 디렉토리 통째로 복사 (또는 git clone)
3. 같은 방식으로 `uvicorn` 띄우기 — 운영용으로는 `--workers 2 --host 0.0.0.0` 정도
4. 앞에 nginx/Caddy 붙이거나, systemd로 데몬화

## 웹서치 / MCP (tool calling)

`/chat` 호출 시 모델이 알아서 도구를 부른다. 부르는 게 없으면 그대로 답하고, 도구 자체가 설정 안 되어 있으면 시스템 메시지로 "외부 도구 없음 — 네 지식만으로 답해" 라고 모델에 알린 뒤 답하게 함. 그래서 키가 없어도 챗봇은 그냥 동작.

도구 레이어는 `tools/`에 분리되어 있어서 나중에 Temporal + ReAct로 감쌀 때 그대로 재사용 가능:

```
tools/
  registry.py     # Tool / ToolRegistry / build_registry
  web_search.py   # Tavily 래퍼 (TAVILY_API_KEY 없으면 None 반환)
  mcp_client.py   # MCPManager — stdio 서버 lifecycle + ClientSession
```

### Tavily (웹서치)

1. https://tavily.com/ 가입 → API 키 발급 (무료 1000 req/월)
2. `.env`에 키 채우기:
   ```bash
   TAVILY_API_KEY=tvly-...
   TAVILY_SEARCH_DEPTH=basic        # 또는 advanced
   TAVILY_MAX_RESULTS=5
   ```
3. 의존성: `tavily-python` (requirements.txt에 추가됨)

### MCP (stdio 로컬 서버)

1. 예시 설정 복사:
   ```bash
   cp mcp.json.example mcp.json
   ```
2. `mcp.json`에서 쓸 서버만 남기고 수정. 예시는 filesystem + fetch 두 개가 들어있음
3. 서버별 실행 도구 필요:
   - `filesystem` → Node (`npx`). `brew install node`
   - `fetch` → `uv` (`uvx`). `brew install uv`
   - 다른 MCP 서버는 https://github.com/modelcontextprotocol/servers 참고
4. 의존성: `mcp` (Anthropic 공식 Python SDK, requirements.txt에 추가됨)
5. `.env`의 `MCP_CONFIG_PATH`로 설정 파일 경로 지정 (기본 `./mcp.json`)

### 동작 확인

- `GET /health` 응답의 `tools` 배열에 활성화된 도구가 들어옴 (`web_search`, `<서버>__<툴>` 형태)
- `POST /chat` 응답에 `tools_used: ["web_search", ...]` 가 같이 옴
- 도구 호출에 의한 추가 라운드트립은 최대 `MAX_TOOL_ITERATIONS` (기본 5)

### 제한 / 메모

- 스트리밍 + tool calling 조합은 지금은 "도구 루프는 non-stream으로 돌고 최종 응답만 SSE로 emit"이라 진짜 토큰 스트림이 아님. 도구 안 쓸 땐 그대로 토큰 스트리밍
- Ollama의 tool calling 지원은 모델별로 편차 큼. Qwen2.5 / Llama 3.1은 잘 됨

## 6. UI (Open WebUI)

Open WebUI를 도커로 띄워 챗 UI + 멀티유저(가입/로그인/세션 분리)를 거의 코드 없이 얹는다. Open WebUI는 모델을 Ollama에 직접 묻지 않고 **이 FastAPI 서버의 `/v1` 엔드포인트**를 거쳐 호출하므로, 도구(웹서치/MCP)와 향후 백엔드 로직(권한·로깅·사내 API)이 UI에도 그대로 적용된다.

```
[브라우저] → Open WebUI(:3000) → FastAPI(:8000)/v1/* → Ollama(:11434)/v1/*
                                       └─ tools/ (Tavily, MCP)
```

### 띄우기

```bash
# 1) FastAPI 서버가 켜져 있어야 함 (다른 터미널에서)
uvicorn main:app --reload --port 8000

# 2) Open WebUI 컨테이너 기동
docker compose up -d

# 3) 브라우저에서 http://localhost:3000 접속
#    첫 가입자가 자동으로 admin이 됨. 그 뒤 가입자는 admin이 승인해야 로그인 가능.
```

확인:

- 모델 선택 드롭다운에 Ollama에 받아둔 모델들이 보이면 연결 OK (FastAPI `/v1/models` 결과)
- 채팅 시 FastAPI 로그에 `/v1/chat/completions` 요청이 찍히고, 도구가 설정되어 있으면 `tools/` 코드 경로를 그대로 탐

### 설정 메모

- `docker-compose.yml`에서 `OPENAI_API_BASE_URLS=http://host.docker.internal:8000/v1`로 호스트의 FastAPI를 가리킴. macOS는 기본, Linux는 `extra_hosts: host.docker.internal:host-gateway`로 자동 매핑됨
- `DEFAULT_USER_ROLE=pending` 이라서 신규 가입자는 admin 승인 전까지 로그인 불가. 사내 자유 가입을 허용하려면 `user`로 바꿔도 됨
- 가입 자체를 닫고 admin이 계정만 직접 만들고 싶으면 `ENABLE_SIGNUP=false`
- `OPENAI_API_KEYS`는 placeholder. FastAPI 쪽에서 키 검사를 하지 않으므로 아무 값이나 OK. 나중에 권한 레이어를 붙이면 여기와 FastAPI 둘 다 같이 손봐야 함
- 컨테이너 데이터(유저 DB, 채팅 히스토리)는 `open-webui-data` 도커 볼륨에 영속. `docker compose down`해도 안 날아감. 완전 초기화하려면 `docker compose down -v`

### 트러블슈팅

- UI에서 모델이 안 보임 → Open WebUI 컨테이너 로그(`docker logs open-webui`)에서 `host.docker.internal:8000` 연결 실패면 FastAPI가 `0.0.0.0`이 아닌 `127.0.0.1`에 바인딩되어 그럴 수 있음. `uvicorn ... --host 0.0.0.0`로 재기동
- 응답이 느림 / 도중에 끊김 → 도구 루프가 도는 중일 수 있음. FastAPI 로그에서 tool_calls 확인

## 다음 단계 (메모)

- **ReAct + Temporal** — `agents/react.py` 만들어 `tools/ToolRegistry`를 받아 도구 호출 루프 구현 → Temporal workflow로 감싸 재시도/재개 가능하게. 지금 `_run_tool_loop`가 그 자리의 임시 구현
- **권한/로깅** — Open WebUI가 호출자 정보를 `/v1/chat/completions`에 함께 넘기게 하려면 FastAPI 쪽에 인증 미들웨어(JWT 검증 등)를 붙이고 Open WebUI의 `OPENAI_API_KEYS`를 사용자별 토큰으로 분리 가능

## 트러블슈팅

- `502 Ollama unreachable` — `ollama serve` 떠 있는지, `OLLAMA_BASE_URL`이 `http://localhost:11434/v1` (끝에 `/v1` 필수) 인지 확인
- 응답이 느림 — 모델이 너무 큼. `qwen2.5:3b` 같은 작은 모델로 내려보기. 또는 `ollama ps`로 메모리 상황 확인
- 처음 요청만 오래 걸림 — 모델이 메모리에 로드되는 중. 한 번 워밍업하면 빨라짐
