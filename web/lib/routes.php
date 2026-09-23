<?php
declare(strict_types=1);
/** 메뉴 · 라우트 정의 (화이트리스트). */
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }

/**
 * 라우트: page key => [view, title, group, account(계좌선택 표시), admin(관리자 전용)]
 */
function app_routes(): array
{
    return [
        'dashboard' => ['view' => 'dashboard', 'title' => '종합현황', 'group' => 'dashboard', 'account' => true],
        'account.balance' => ['view' => 'account_balance', 'title' => '예수금 · 잔고', 'group' => 'account', 'account' => true],
        'account.holdings' => ['view' => 'account_holdings', 'title' => '보유종목', 'group' => 'account', 'account' => true],
        'trade.executions' => ['view' => 'trade_executions', 'title' => '체결내역', 'group' => 'trade', 'account' => true],
        'trade.orders' => ['view' => 'trade_orders', 'title' => '주문내역', 'group' => 'trade', 'account' => true],
        'trade.ledger' => ['view' => 'trade_ledger', 'title' => '거래내역', 'group' => 'trade', 'account' => true],
        'trade.daily' => ['view' => 'trade_daily', 'title' => '매매일지', 'group' => 'trade', 'account' => true],
        'trade.analysis' => ['view' => 'trade_analysis', 'title' => '거래 분석', 'group' => 'trade'],
        'strategy.algorithms' => ['view' => 'strategy_algorithms', 'title' => '알고리즘 현황', 'group' => 'strategy'],
        'strategy.params' => ['view' => 'strategy_params', 'title' => '파라미터 · 변경이력', 'group' => 'strategy'],
        'strategy.signals' => ['view' => 'strategy_signals', 'title' => '신호 기록', 'group' => 'strategy'],
        'strategy.claude' => ['view' => 'strategy_claude', 'title' => 'Claude 판단', 'group' => 'strategy'],
        'strategy.trend' => ['view' => 'strategy_trend', 'title' => '산업 트렌드', 'group' => 'strategy'],
        /* 리서치: DART 재무데이터 + Claude 재무분석 리포트 조회(순수 참고용).
         * 계좌 · 주문과 전혀 무관하므로 'account' 를 지정하지 않는다(전역 화면). */
        'research.reports' => ['view' => 'research_reports', 'title' => '재무분석 리포트', 'group' => 'research'],
        'research.company' => ['view' => 'research_company', 'title' => '기업 재무분석', 'group' => 'research'],
        'system.status' => ['view' => 'system_status', 'title' => '서버 상태', 'group' => 'system'],
        'system.events' => ['view' => 'system_events', 'title' => '이벤트 로그', 'group' => 'system'],
        'system.archive' => ['view' => 'system_archive', 'title' => '이벤트 · API 오류 보관', 'group' => 'system'],
        'system.profile' => ['view' => 'system_profile', 'title' => '내 정보 · 비밀번호 변경', 'group' => 'system'],
        'system.users' => ['view' => 'system_users', 'title' => '사용자 목록', 'group' => 'system', 'admin' => true],
        /* 관리자 전용 쓰기 화면(2026-09-23 도입) — auth_is_admin() 서버사이드 강제는 index.php 라우팅에서
         * $route['admin'] 플래그로 이미 걸리며, 각 POST 핸들러에서도 별도로 다시 검사한다(방어적 이중 확인). */
        'control.algorithms' => ['view' => 'control_algorithms', 'title' => '알고리즘 관리', 'group' => 'system', 'admin' => true],
        'control.auto_trading' => ['view' => 'control_auto_trading', 'title' => '자동거래 제어', 'group' => 'system', 'admin' => true],
    ];
}

/** 상단 대메뉴 → 소메뉴. */
function app_menu(): array
{
    return [
        'dashboard' => ['label' => '대시보드', 'items' => ['dashboard' => '종합현황']],
        'account' => ['label' => '계좌', 'items' => [
            'account.balance' => '예수금 · 잔고',
            'account.holdings' => '보유종목',
        ]],
        'trade' => ['label' => '거래', 'items' => [
            'trade.executions' => '체결내역',
            'trade.orders' => '주문내역',
            'trade.ledger' => '거래내역',
            'trade.daily' => '매매일지',
            'trade.analysis' => '거래 분석',
        ]],
        'strategy' => ['label' => '전략', 'items' => [
            'strategy.algorithms' => '알고리즘 현황',
            'strategy.params' => '파라미터 · 변경이력',
            'strategy.signals' => '신호 기록',
            'strategy.claude' => 'Claude 판단',
            'strategy.trend' => '산업 트렌드',
        ]],
        'research' => ['label' => '리서치', 'items' => [
            'research.reports' => '재무분석 리포트',
            'research.company' => '기업 재무분석',
        ]],
        'system' => ['label' => '시스템', 'items' => [
            'system.status' => '서버 상태',
            'system.events' => '이벤트 로그',
            'system.archive' => '이벤트 · API 오류 보관',
            'system.profile' => '내 정보',
            'system.users' => '사용자 목록',
            'control.algorithms' => '알고리즘 관리',
            'control.auto_trading' => '자동거래 제어',
        ]],
    ];
}
