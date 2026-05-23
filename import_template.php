<?php
/**
 * ftp-deploy-shared — one-shot SQL migration helper.
 *
 * Receives a base64-encoded JSON payload uploaded via FTP (db-data.b64),
 * executes the schema DDL (tolerating "Duplicate column"/"already exists" for
 * idempotent re-runs) and then INSERTs every seed row via PDO prepared
 * statements (so MySQL never parses our body text as SQL).
 *
 * Triggered via HTTPS GET with only a random token in the URL — no POST body
 * for Cloudflare/WAF to inspect.
 *
 * On success, this file and db-data.b64 self-destruct from the server.
 *
 * Placeholders rewritten by deploy.py at upload time:
 *   __TOKEN__   — per-run random URL-safe token
 *   __DB_HOST__, __DB_NAME__, __DB_USER__, __DB_PASS__ — from .env
 */

header('Content-Type: application/json; charset=utf-8');

$TOKEN = '__TOKEN__';
$given = isset($_GET['token']) ? $_GET['token'] : '';
if (!hash_equals($TOKEN, $given)) {
    http_response_code(403);
    echo json_encode(array('ok' => false, 'error' => 'bad token'));
    exit;
}

$dataFile = __DIR__ . '/db-data.b64';
if (!is_file($dataFile)) {
    echo json_encode(array('ok' => false, 'error' => 'db-data.b64 not found'));
    exit;
}

$raw = base64_decode(file_get_contents($dataFile));
if ($raw === false) {
    echo json_encode(array('ok' => false, 'error' => 'base64 decode failed'));
    exit;
}
$body = json_decode($raw, true);
if (!is_array($body)) {
    echo json_encode(array('ok' => false, 'error' => 'invalid JSON payload'));
    exit;
}

try {
    $pdo = new PDO(
        'mysql:host=__DB_HOST__;dbname=__DB_NAME__;charset=utf8mb4',
        '__DB_USER__',
        '__DB_PASS__',
        array(PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION)
    );
} catch (PDOException $e) {
    echo json_encode(array('ok' => false, 'error' => 'DB connect: ' . $e->getMessage()));
    exit;
}

$report = array(
    'schema_ok'     => 0,
    'schema_ignored'=> 0,
    'schema_errors' => array(),
    'rows_inserted' => array(),
    'errors'        => array(),
);

/* ---- 1. Schema DDL — statement-by-statement, tolerate dup-column/exists ---- */
$schema = isset($body['schema']) ? $body['schema'] : '';
// Split on `;` at end of line — safe for typical DDL (CREATE/ALTER don't end body text with `;` + newline).
foreach (preg_split('/;[ \t]*\r?\n/', $schema) as $stmt) {
    $stmt = trim($stmt);
    if ($stmt === '' || strncmp($stmt, '--', 2) === 0) {
        continue;
    }
    try {
        $pdo->exec($stmt);
        $report['schema_ok']++;
    } catch (PDOException $e) {
        $msg = $e->getMessage();
        if (strpos($msg, 'Duplicate column') !== false || strpos($msg, 'already exists') !== false) {
            // Re-run of the same migration — harmless, ignore.
            $report['schema_ignored']++;
        } else {
            $report['schema_errors'][] = array(
                'err' => $msg,
                'sql' => substr($stmt, 0, 120),
            );
        }
    }
}

/* ---- 2. Seed rows — $body['tables'] is a list of {name, columns, rows} ---- */
$tables = isset($body['tables']) && is_array($body['tables']) ? $body['tables'] : array();
foreach ($tables as $t) {
    $name    = isset($t['name'])    ? $t['name']    : '';
    $columns = isset($t['columns']) ? $t['columns'] : array();
    $rows    = isset($t['rows'])    ? $t['rows']    : array();
    if (!is_string($name) || !preg_match('/^[A-Za-z_][A-Za-z0-9_]*$/', $name)) {
        $report['errors'][] = "invalid table name: " . print_r($name, true);
        continue;
    }
    if (!is_array($columns) || !is_array($rows) || empty($rows)) {
        continue;
    }
    foreach ($columns as $c) {
        if (!is_string($c) || !preg_match('/^[A-Za-z_][A-Za-z0-9_]*$/', $c)) {
            $report['errors'][] = "invalid column name in $name: " . print_r($c, true);
            continue 2;
        }
    }
    // Idempotent re-seed: clear the table first (remove if you want append-only).
    try {
        $pdo->exec("DELETE FROM `$name`");
    } catch (PDOException $e) {
        $report['errors'][] = "DELETE FROM $name: " . $e->getMessage();
        continue;
    }
    // Build INSERT INTO `tbl` (`c1`, `c2`, ...) VALUES (?, ?, ...) with placeholders.
    $col_list = '`' . implode('`, `', $columns) . '`';
    $placeholders = implode(', ', array_fill(0, count($columns), '?'));
    try {
        $ins = $pdo->prepare("INSERT INTO `$name` ($col_list) VALUES ($placeholders)");
        $count = 0;
        foreach ($rows as $row) {
            // Each value bound as a parameter — MySQL never parses our body text as SQL.
            $ins->execute($row);
            $count++;
        }
        $report['rows_inserted'][$name] = $count;
    } catch (PDOException $e) {
        $report['errors'][] = "INSERT INTO $name: " . $e->getMessage();
    }
}

$report['ok'] = empty($report['errors']) && empty($report['schema_errors']);
echo json_encode($report, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);

/* ---- 3. Self-destruct on success ---- */
if ($report['ok']) {
    @unlink($dataFile);
    @unlink(__FILE__);
}
