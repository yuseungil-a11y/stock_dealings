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
        'system.status' => ['view' => 'system_status', 'title' => '서버 상태', 'group' => 'system'],
        'system.events' => ['view' => 'system_events', 'title' => '이벤트 로그', 'group' => 'system'],
        'system.archive' => ['view' => 'system_archive', 'title' => '이벤트 · API 오류 보관', 'group' => 'system'],
        'system.profile' => ['view' => 'system_profile', 'title' => '내 정보 · 비밀번호 변경', 'group' => 'system'],
        'system.users' => ['view' => 'system_users', 'title' => '사용자 목록', 'group' => 'system', 'admin' => true],
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
        'system' => ['label' => '시스템', 'items' => [
            'system.status' => '서버 상태',
            'system.events' => '이벤트 로그',
            'system.archive' => '이벤트 · API 오류 보관',
            'system.profile' => '내 정보',
            'system.users' => '사용자 목록',
        ]],
    ];
}
