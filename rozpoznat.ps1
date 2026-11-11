$ErrorActionPreference = "SilentlyContinue"
$gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name })
$cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name
$bytes = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory
$ram = 0
if ($bytes) { $ram = [int][math]::Round($bytes / 1GB) }
$gpuText = ($gpus | Where-Object { $_ }) -join " | "

$npu = @()
try {
    $npu = @(Get-PnpDevice | Where-Object {
        $_.FriendlyName -match "NPU|AI Boost|Neural"
    })
} catch {
    $npu = @()
}

$profile = "INTEL"
if ($gpuText -match "NVIDIA") {
    $profile = "NVIDIA"
} elseif ($gpuText -match "Radeon|AMD") {
    $profile = "AMD"
} elseif ($cpu -match "Nova Lake" -or $gpuText -match "Arc" -or $npu.Count -gt 0) {
    $profile = "INTEL_AI"
}

[Console]::Error.WriteLine("Procesor: $cpu")
[Console]::Error.WriteLine("Grafika: $gpuText")
[Console]::Error.WriteLine("Pamet: $ram GB")
[Console]::Error.WriteLine("Profil: $profile")
Write-Output $profile
