param(
    [Parameter(Mandatory = $true)][string]$TitleLike,
    [Parameter(Mandatory = $true)][string]$Out,
    [int]$WaitSeconds = 6
)

Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class PevCapture {
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdcBlt, uint nFlags);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
"@

$deadline = (Get-Date).AddSeconds($WaitSeconds)
$handle = [IntPtr]::Zero
while ((Get-Date) -lt $deadline) {
    $proc = Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -like "*$TitleLike*"
    } | Select-Object -First 1
    if ($proc) { $handle = $proc.MainWindowHandle; break }
    Start-Sleep -Milliseconds 300
}
if ($handle -eq [IntPtr]::Zero) { Write-Error "window not found: $TitleLike"; exit 2 }

[PevCapture]::ShowWindow($handle, 9) | Out-Null
[PevCapture]::SetForegroundWindow($handle) | Out-Null
Start-Sleep -Milliseconds 900

$rect = New-Object PevCapture+RECT
[PevCapture]::GetWindowRect($handle, [ref]$rect) | Out-Null
$width = $rect.Right - $rect.Left
$height = $rect.Bottom - $rect.Top
if ($width -le 0 -or $height -le 0) { Write-Error "bad window rect"; exit 3 }

$bitmap = New-Object System.Drawing.Bitmap $width, $height
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$hdc = $graphics.GetHdc()
$ok = [PevCapture]::PrintWindow($handle, $hdc, 2)
$graphics.ReleaseHdc($hdc)
$graphics.Dispose()
$bitmap.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$bitmap.Dispose()
Write-Output "captured=$ok size=${width}x${height} -> $Out"
