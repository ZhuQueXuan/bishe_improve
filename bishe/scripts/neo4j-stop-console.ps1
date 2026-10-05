param([Parameter(Mandatory=$true)][int]$ProcessId)
$ErrorActionPreference = 'Stop'
$target = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId"
if (-not $target -or $target.Name -notmatch '^powershell.exe$' -or $target.CommandLine -notmatch 'neo4j') {
    throw 'Target is not the recorded dedicated Neo4j console.'
}
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class Neo4jConsoleSignal {
 [DllImport("kernel32.dll", SetLastError=true)] public static extern bool FreeConsole();
 [DllImport("kernel32.dll", SetLastError=true)] public static extern bool AttachConsole(uint pid);
 [DllImport("kernel32.dll", SetLastError=true)] public static extern bool SetConsoleCtrlHandler(IntPtr handler, bool add);
 [DllImport("kernel32.dll", SetLastError=true)] public static extern bool GenerateConsoleCtrlEvent(uint type, uint group);
}
'@
[Neo4jConsoleSignal]::FreeConsole() | Out-Null
if (-not [Neo4jConsoleSignal]::AttachConsole([uint32]$ProcessId)) { throw 'Cannot attach Neo4j console.' }
[Neo4jConsoleSignal]::SetConsoleCtrlHandler([IntPtr]::Zero, $true) | Out-Null
if (-not [Neo4jConsoleSignal]::GenerateConsoleCtrlEvent(0, 0)) { throw 'Cannot request graceful shutdown.' }
[Neo4jConsoleSignal]::FreeConsole() | Out-Null
