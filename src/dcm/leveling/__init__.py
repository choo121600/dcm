"""활동 레벨링 시스템 (activity leveling).

멤버의 텍스트 메시지를 LLM 없는 휴리스틱으로 질-가중해 누적 XP 로 적립하고,
레벨은 저장하지 않고 조회 시 순수함수로 산정한다. 레벨을 '신뢰 등급' 으로 삼아
웹검색·LLM 대화의 일일 한도를 차등하는 게 주 보상, 레벨→역할 자동부여가 보조.

저장은 memory.db 와 분리된 leveling.db (WAL) 에 단일 전용-스레드 writer 로 직렬화한다.
"""
from __future__ import annotations
