<?php
// Sent via SSH stdin, never installed remotely. stdout is captured in runner RAM.
// Mode is substituted only from the fixed list in dev_runner.py.
ini_set('display_errors', '0');
error_reporting(0);
try {
    $mode = '__MODE__';
    if (!in_array($mode, ['prod', 'profile', 'counter'], true)) { throw new RuntimeException(); }
    $root = $mode === 'prod' ? '/var/www/dwl/data/www/adm.cactushub.app' : '/var/www/dwl/data/www/adm-dev.cactus.dewil.ru';
    chdir($root);
    require $root.'/vendor/autoload.php';
    $app = require $root.'/bootstrap/app.php';
    $app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
    if ($mode === 'prod') {
        $hosts = [];
        foreach (App\Models\HiddifyServer::all() as $row) {
            foreach ([$row->domain, $row->ssh_host, parse_url((string)$row->subscription_origin, PHP_URL_HOST), parse_url((string)$row->api_base_url, PHP_URL_HOST)] as $host) {
                $host = strtolower(trim((string)$host, '[]'));
                if ($host !== '') { $hosts[] = $host; }
            }
        }
        $hosts = array_values(array_unique($hosts));
        if (!$hosts) { throw new RuntimeException(); }
        echo json_encode(['ok'=>true, 'prod_hosts'=>$hosts]);
        exit;
    }
    $node = App\Models\HiddifyServer::findOrFail(42);
    $users = App\Models\User::where('is_probe', true)->get();
    if ($node->domain !== 'de4.cactushub.app' || !$node->is_active || $users->count() !== 1) { throw new RuntimeException(); }
    $user = $users->first();
    if (!$user->api_key) { throw new RuntimeException(); }
    if ($mode === 'profile') {
        $plan = $app->make(App\Services\Monitoring\LineProbe\Plan::class)->build();
        $nodes = [];
        foreach ($plan['contours'] as $contour) { foreach ($contour['nodes'] as $entry) { $nodes[] = $entry; } }
        if (count($nodes) !== 1) { throw new RuntimeException(); }
        $p = $nodes[0];
        $uri = parse_url($p['uri']);
        parse_str($uri['query'] ?? '', $query);
        if ($p['node_id'] !== 42 || $p['host'] !== 'de4.cactushub.app' || $p['port'] !== 443 ||
            ($uri['scheme'] ?? '') !== 'vless' || ($query['security'] ?? '') !== 'tls' ||
            !hash_equals((string)$user->api_key, rawurldecode($uri['user'] ?? ''))) { throw new RuntimeException(); }
        echo json_encode(['ok'=>true, 'node_id'=>42, 'host'=>'de4.cactushub.app', 'active'=>true, 'uri'=>$p['uri']]);
        exit;
    }
    $base = rtrim((string)$node->api_base_url, '/');
    if (parse_url($base, PHP_URL_SCHEME) !== 'https' || parse_url($base, PHP_URL_HOST) !== 'de4.cactushub.app') { throw new RuntimeException(); }
    $response = Illuminate\Support\Facades\Http::connectTimeout(5)->timeout(10)
        ->withOptions(['allow_redirects'=>false])
        ->withHeaders(['Hiddify-API-Key'=>(string)$node->api_key])
        ->get($base.'/api/v2/admin/user/');
    if ($response->status() !== 200) { throw new RuntimeException(); }
    $rows = $response->json();
    if (!is_array($rows) || !array_is_list($rows)) { throw new RuntimeException(); }
    $selected = array_values(array_filter($rows, fn($r)=>is_array($r) && ($r['uuid'] ?? null) === $user->api_key));
    if (count($selected) !== 1) { throw new RuntimeException(); }
    $r = $selected[0];
    if (isset($r['current_usage']) && is_int($r['current_usage'])) { $bytes = $r['current_usage']; }
    elseif (isset($r['current_usage_GB']) && (is_int($r['current_usage_GB']) || is_float($r['current_usage_GB']))) { $bytes = $r['current_usage_GB'] * (1024 ** 3); }
    else { throw new RuntimeException(); }
    if (!is_finite((float)$bytes) || $bytes < 0) { throw new RuntimeException(); }
    // Absolute counter is private RAM input; public evidence contains only delta.
    echo json_encode(['ok'=>true, 'counter_bytes'=>$bytes, 'sampled_at_ms'=>(int)floor(microtime(true)*1000)]);
} catch (Throwable $e) {
    echo '{"ok":false,"code":"DEV_ADAPTER_FAILED"}';
    exit(2);
}
