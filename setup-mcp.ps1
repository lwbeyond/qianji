param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$mcpDirectory = Join-Path $PSScriptRoot '.local\mcp-venv'
$mcpPython = Join-Path $mcpDirectory 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $mcpPython)) {
    & $Python -m venv $mcpDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create isolated MCP environment.' }
}
& $mcpPython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements-mcp.lock')
if ($LASTEXITCODE -ne 0) { throw 'Cannot install locked MCP dependencies.' }
& $mcpPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'MCP dependency check failed.' }
Write-Host 'Source MCP environment ready. No financial import, export or database was opened.'
