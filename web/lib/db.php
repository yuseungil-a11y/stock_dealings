<?php
declare(strict_types=1);
/** PDO 연결 (prepared statement 전용, 에뮬레이션 OFF). 웹은 stock_web 계정만 사용. */
if (!defined('STOCK_APP')) { http_response_code(404); exit('Not Found'); }

function db(): PDO
{
    static $pdo = null;
    if ($pdo instanceof PDO) {
        return $pdo;
    }
    $db = app_config()['db'];
    $dsn = sprintf(
        'mysql:host=%s;port=%d;dbname=%s;charset=utf8mb4',
        (string)($db['host'] ?? '127.0.0.1'),
        (int)($db['port'] ?? 3306),
        (string)($db['name'] ?? '')
    );
    try {
        $pdo = new PDO($dsn, (string)($db['user'] ?? ''), (string)($db['password'] ?? ''), [
            PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            PDO::ATTR_EMULATE_PREPARES => false,
            PDO::ATTR_STRINGIFY_FETCHES => false,
            PDO::MYSQL_ATTR_MULTI_STATEMENTS => false,
        ]);
    } catch (PDOException $e) {
        // 예외 메시지에 접속정보가 섞일 수 있으므로 코드만 로그에 남긴다.
        app_fatal('db_connect_failed', '데이터베이스에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.',
            'PDO code=' . $e->getCode());
    }
    return $pdo;
}

/**
 * prepare + 타입 바인딩.
 * 에뮬레이션이 꺼져 있으면 LIMIT / INTERVAL 자리의 정수는 PARAM_INT 로 바인딩해야 한다.
 */
function db_stmt(string $sql, array $params = []): PDOStatement
{
    $st = db()->prepare($sql);
    $i = 0;
    foreach ($params as $key => $value) {
        $name = is_int($key) ? ++$i : $key;
        if (is_int($value) || is_bool($value)) {
            $st->bindValue($name, (int)$value, PDO::PARAM_INT);
        } elseif ($value === null) {
            $st->bindValue($name, null, PDO::PARAM_NULL);
        } else {
            $st->bindValue($name, (string)$value, PDO::PARAM_STR);
        }
    }
    $st->execute();
    return $st;
}

/** 조회 헬퍼 — 값은 반드시 바인딩으로만 전달한다. */
function db_all(string $sql, array $params = []): array
{
    return db_stmt($sql, $params)->fetchAll();
}

function db_row(string $sql, array $params = []): ?array
{
    $row = db_stmt($sql, $params)->fetch();
    return $row === false ? null : $row;
}

function db_val(string $sql, array $params = [], mixed $default = null): mixed
{
    $v = db_stmt($sql, $params)->fetchColumn();
    return $v === false ? $default : $v;
}

function db_exec(string $sql, array $params = []): int
{
    return db_stmt($sql, $params)->rowCount();
}

/** DB 연결 가능 여부(관제 화면용). */
function db_ping(): bool
{
    try {
        db()->query('SELECT 1');
        return true;
    } catch (Throwable $e) {
        return false;
    }
}
