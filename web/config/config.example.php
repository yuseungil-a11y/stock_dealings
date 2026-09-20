<?php
/**
 * 웹 설정 예시. 복사해서 config.local.php 로 만들어 사용한다(git 제외).
 * 배포 시 deploy_web.ps1 이 config.local.php 를 웹루트 밖
 * D:\xampp\stock_private\config.local.php 로 복사하며, 앱은 그 경로를 우선 사용한다.
 *
 * DB 계정은 반드시 웹 전용 계정(stock_web: SELECT + 로그인 관련 최소 쓰기)을 쓴다.
 * root / stock_svr 계정은 웹에서 사용하지 않는다.
 */
return [
    'db' => [
        'host' => '127.0.0.1',
        'port' => 4406,
        'name' => 'stock_dealings',
        'user' => 'stock_web',
        'password' => '',
    ],
];
