<#
.SYNOPSIS
    웹 소스(web\)를 xampp 웹루트(D:\xampp\htdocs\stock)로 배포한다.

.DESCRIPTION
    * robocopy /MIR 미러 배포 (재실행 안전).
    * config\ 는 웹루트로 복사하지 않고, config.local.php 만 웹루트 밖
      D:\xampp\stock_private\ 로 복사한다(앱이 이 경로를 우선 사용).
    * 오류 로그 디렉터리 D:\xampp\stock_private\logs 를 준비한다.
    * Apache/PHP 설정 파일은 건드리지 않으며 Apache 를 재시작하지 않는다.
    * 배포 후 http://localhost/stock/ 스모크 테스트(curl)를 수행한다.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File D:\claude_stock_dealings\tools\deploy_web.ps1
    powershell -ExecutionPolicy Bypass -File ...\deploy_web.ps1 -SkipSmoke
#>
[CmdletBinding()]
param(
    [string]$Source      = 'D:\claude_stock_dealings\web',
    [string]$Target      = 'D:\xampp\htdocs\stock',
    [string]$PrivateDir  = 'D:\xampp\stock_private',
    [string]$SmokeUrl    = 'http://localhost/stock/',
    [switch]$SkipSmoke
)

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "[deploy] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "[  ok  ] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "[ warn ] $msg" -ForegroundColor Yellow }

# ---------------------------------------------------------------- 사전 점검
if (-not (Test-Path -LiteralPath $Source)) {
    throw "웹 소스 경로를 찾을 수 없습니다: $Source"
}
$cfgSrc = Join-Path $Source 'config\config.local.php'
if (-not (Test-Path -LiteralPath $cfgSrc)) {
    throw "설정 파일이 없습니다: $cfgSrc  (config.example.php 를 복사해 만드세요)"
}

Write-Step "소스   : $Source"
Write-Step "웹루트 : $Target"
Write-Step "비공개 : $PrivateDir"

# ---------------------------------------------------------------- 디렉터리 준비
foreach ($d in @($Target, $PrivateDir, (Join-Path $PrivateDir 'logs'))) {
    if (-not (Test-Path -LiteralPath $d)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
        Write-Ok "디렉터리 생성: $d"
    }
}

# ---------------------------------------------------------------- 미러 배포
# 제외: config 폴더(비밀 설정), 개발 문서, 로그/백업/임시 파일
$excludeDirs  = @('config', '.git', 'node_modules', 'tests')
$excludeFiles = @('README.md', '*.log', '*.bak', '*.tmp', '*.orig', 'Thumbs.db', 'desktop.ini', '*.local.php')

$rcArgs = @($Source, $Target, '/MIR', '/NFL', '/NDL', '/NJH', '/NP', '/R:2', '/W:1')
$rcArgs += '/XD'; $rcArgs += $excludeDirs
$rcArgs += '/XF'; $rcArgs += $excludeFiles

Write-Step 'robocopy 미러 실행'
& robocopy @rcArgs | Out-Null
$rc = $LASTEXITCODE
if ($rc -ge 8) {
    throw "robocopy 실패 (exit=$rc)"
}
Write-Ok "미러 배포 완료 (robocopy exit=$rc)"

# 혹시 과거 배포로 웹루트에 남아있을 수 있는 설정 폴더 제거
$strayCfg = Join-Path $Target 'config'
if (Test-Path -LiteralPath $strayCfg) {
    Remove-Item -LiteralPath $strayCfg -Recurse -Force
    Write-Ok "웹루트의 config 폴더 제거: $strayCfg"
}

# ---------------------------------------------------------------- 설정 파일 배치
$cfgDst = Join-Path $PrivateDir 'config.local.php'
Copy-Item -LiteralPath $cfgSrc -Destination $cfgDst -Force
Write-Ok "설정 복사: $cfgDst (웹루트 밖)"

# ---------------------------------------------------------------- 결과 요약
$deployed = Get-ChildItem -LiteralPath $Target -Recurse -File -Force
Write-Ok ("배포 파일 수: {0}" -f $deployed.Count)
$entry = @('index.php', 'api.php', '.htaccess', 'assets\css\app.css', 'assets\js\app.js')
foreach ($f in $entry) {
    $p = Join-Path $Target $f
    if (Test-Path -LiteralPath $p) { Write-Ok "확인: $f" } else { Write-Warn2 "누락: $f" }
}
if (Test-Path -LiteralPath (Join-Path $Target 'config')) {
    Write-Warn2 '경고: 웹루트에 config 폴더가 존재합니다.'
}

# ---------------------------------------------------------------- 스모크 테스트
if ($SkipSmoke) {
    Write-Step '스모크 테스트 생략(-SkipSmoke)'
    return
}

Write-Step "스모크 테스트: $SmokeUrl"
$curl = 'curl.exe'
try { $null = & $curl --version 2>$null } catch { Write-Warn2 'curl.exe 없음 — 스모크 테스트 생략'; return }

$checks = @(
    @{ Name = '루트(로그인 리다이렉트)'; Url = $SmokeUrl;                          Expect = @('302','303','200') },
    @{ Name = '로그인 화면';            Url = "${SmokeUrl}index.php?p=login";      Expect = @('200') },
    @{ Name = 'CSS';                    Url = "${SmokeUrl}assets/css/app.css";     Expect = @('200') },
    @{ Name = 'JS';                     Url = "${SmokeUrl}assets/js/app.js";       Expect = @('200') },
    @{ Name = 'API 미인증(401)';        Url = "${SmokeUrl}api.php?r=ping";         Expect = @('401') },
    @{ Name = 'Claude 판단 미인증';     Url = "${SmokeUrl}index.php?p=strategy.claude"; Expect = @('302','303') },
    @{ Name = '보유종목 미인증';        Url = "${SmokeUrl}index.php?p=account.holdings"; Expect = @('302','303') },
    @{ Name = '거래 분석 미인증';       Url = "${SmokeUrl}index.php?p=trade.analysis"; Expect = @('302','303') },
    @{ Name = '산업 트렌드 미인증';     Url = "${SmokeUrl}index.php?p=strategy.trend"; Expect = @('302','303') },
    @{ Name = '보관 화면 미인증';       Url = "${SmokeUrl}index.php?p=system.archive"; Expect = @('302','303') },
    @{ Name = '재무분석 목록 미인증';   Url = "${SmokeUrl}index.php?p=research.reports"; Expect = @('302','303') },
    @{ Name = '기업 재무분석 미인증';   Url = "${SmokeUrl}index.php?p=research.company"; Expect = @('302','303') },
    @{ Name = '리서치 API 미인증';      Url = "${SmokeUrl}api.php?r=research_company&stk=005930"; Expect = @('401') },
    @{ Name = '분석 내보내기 미인증';   Url = "${SmokeUrl}index.php?p=trade.analysis&export=csv"; Expect = @('302','303') },
    @{ Name = 'lib 직접접근 차단';      Url = "${SmokeUrl}lib/db.php";             Expect = @('403','404') },
    @{ Name = 'views 직접접근 차단';    Url = "${SmokeUrl}views/layout.php";       Expect = @('403','404') },
    @{ Name = 'config 미배포';          Url = "${SmokeUrl}config/config.local.php";Expect = @('403','404') },
    @{ Name = '디렉터리 리스팅 차단';   Url = "${SmokeUrl}assets/";                Expect = @('403','404') },
    # 재조사 요청(쓰기)은 CSRF 토큰 없는 POST 를 반드시 403 으로 막아야 한다.
    @{ Name = '재조사 POST CSRF 누락';  Url = "${SmokeUrl}index.php";              Expect = @('403');
       Post = 'action=trend_rescan' }
)

$fail = 0
foreach ($c in $checks) {
    if ($c.ContainsKey('Post')) {
        $code = (& $curl -s -o NUL -w '%{http_code}' -X POST -d $c.Post $c.Url) 2>$null
    } else {
        $code = (& $curl -s -o NUL -w '%{http_code}' $c.Url) 2>$null
    }
    if ($c.Expect -contains $code) {
        Write-Ok ("{0,-24} HTTP {1}" -f $c.Name, $code)
    } else {
        Write-Warn2 ("{0,-24} HTTP {1}  (기대: {2})" -f $c.Name, $code, ($c.Expect -join '/'))
        $fail++
    }
}

if ($fail -eq 0) {
    Write-Ok '스모크 테스트 전체 통과'
} else {
    Write-Warn2 "스모크 테스트 $fail 건 실패 — 위 항목을 확인하세요."
}
