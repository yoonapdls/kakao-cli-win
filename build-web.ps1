Set-Location -LiteralPath (Join-Path $PSScriptRoot "web")
npm.cmd install
npm.cmd run build

