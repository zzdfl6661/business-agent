param(
    [int]$DatabasePort = 3307,
    [int]$Port = 8001
)

# Docker MySQL 映射到 127.0.0.1:3307；Collector 必须在 Windows 宿主机执行。
$env:BIZ_DB_HOST = "127.0.0.1"
$env:BIZ_DB_PORT = "$DatabasePort"

& "$PSScriptRoot\..\.venv\Scripts\python.exe" -m uvicorn collector_main:app --host 127.0.0.1 --port $Port
