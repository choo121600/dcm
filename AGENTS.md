# AGENTS.md

이 저장소에서 작업하는 모든 에이전트(Claude 등)가 따라야 하는 규칙.

## Git / 커밋

- 커밋의 author/committer는 **계정 주인 본인(Yeonguk <choo121600@gmail.com>)만** 사용한다.
  Claude 등 에이전트를 작성자로 넣지 않는다.
- 커밋 메시지와 PR 본문에 에이전트를 co-author로 넣지 않는다.
  구체적으로 다음을 **절대 추가하지 않는다**:
  - `Co-Authored-By: Claude ...` 트레일러
  - `Claude-Session: ...` 트레일러
  - PR 본문의 "🤖 Generated with Claude Code" 류 문구
