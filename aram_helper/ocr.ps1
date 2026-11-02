# 화면(또는 이미지 파일)의 가운데 영역을 Windows 기본 OCR(한국어)로 읽어 줄 단위 JSON으로 출력한다.
# 게임 메모리나 화면 그리기에는 손대지 않고, 화면 캡처만 한다 (OBS·화면 공유와 같은 방식).
#
#   powershell -File ocr.ps1                       주 모니터 캡처
#   powershell -File ocr.ps1 -Path image.png       이미지 파일
#
# 출력: {"width":..,"height":..,"lines":[{"text":..,"x":..,"y":..,"w":..,"h":..}]}
# 좌표는 잘라낸 영역 기준 0~1 비율이다.
param(
    [string]$Path = "",
    [int]$Monitor = -1,          # -1이면 주 모니터
    [double]$Left = 0.18, [double]$Top = 0.12, [double]$Right = 0.86, [double]$Bottom = 0.72,
    [int]$TargetWidth = 2400     # 글자가 작으면 OCR이 틀리므로 이 폭까지 확대한다
)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Runtime.WindowsRuntime
# 화면 배율(125%, 150%)이 있어도 실제 픽셀로 캡처한다
Add-Type -Namespace Native -Name Dpi -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();'
[void][Native.Dpi]::SetProcessDPIAware()

if ($Path) {
    $source = [System.Drawing.Bitmap]::FromFile((Resolve-Path $Path))
} else {
    $screens = [System.Windows.Forms.Screen]::AllScreens
    $screen = if ($Monitor -ge 0 -and $Monitor -lt $screens.Count) { $screens[$Monitor] } else { [System.Windows.Forms.Screen]::PrimaryScreen }
    $b = $screen.Bounds
    $source = New-Object System.Drawing.Bitmap $b.Width, $b.Height
    $g = [System.Drawing.Graphics]::FromImage($source)
    $g.CopyFromScreen($b.X, $b.Y, 0, 0, $source.Size)
    $g.Dispose()
}

# 가운데 영역을 잘라 확대
$cx = [int]($source.Width * $Left); $cy = [int]($source.Height * $Top)
$cw = [int]($source.Width * ($Right - $Left)); $ch = [int]($source.Height * ($Bottom - $Top))
$scale = [Math]::Max(1.0, $TargetWidth / $cw)
$out = New-Object System.Drawing.Bitmap ([int]($cw * $scale)), ([int]($ch * $scale))
$g = [System.Drawing.Graphics]::FromImage($out)
$g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$g.DrawImage($source, (New-Object System.Drawing.Rectangle 0, 0, $out.Width, $out.Height),
             (New-Object System.Drawing.Rectangle $cx, $cy, $cw, $ch), [System.Drawing.GraphicsUnit]::Pixel)
$g.Dispose(); $source.Dispose()
$tmp = Join-Path $env:TEMP ("aram_ocr_" + [guid]::NewGuid().ToString("N") + ".png")
$out.Save($tmp, [System.Drawing.Imaging.ImageFormat]::Png)
$width = $out.Width; $height = $out.Height
$out.Dispose()

# WinRT 비동기 호출을 기다리는 도우미 (Windows PowerShell 5.1)
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) {
    $task = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
    [void]$task.Wait(-1)
    $task.Result
}
[void][Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
[void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
[void][Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]

try {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($tmp)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new("ko"))
    if ($null -eq $engine) { throw "한국어 OCR을 사용할 수 없습니다 (설정 > 시간 및 언어 > 언어에서 한국어 추가)" }
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    $stream.Dispose()

    $lines = foreach ($line in $result.Lines) {
        $rects = $line.Words | ForEach-Object { $_.BoundingRect }
        $x1 = ($rects | Measure-Object -Property X -Minimum).Minimum
        $y1 = ($rects | Measure-Object -Property Y -Minimum).Minimum
        $x2 = ($rects | ForEach-Object { $_.X + $_.Width } | Measure-Object -Maximum).Maximum
        $y2 = ($rects | ForEach-Object { $_.Y + $_.Height } | Measure-Object -Maximum).Maximum
        [ordered]@{ text = $line.Text; x = $x1 / $width; y = $y1 / $height
                    w = ($x2 - $x1) / $width; h = ($y2 - $y1) / $height }
    }
    [ordered]@{ width = $width; height = $height; lines = @($lines) } | ConvertTo-Json -Depth 4 -Compress
} finally {
    Remove-Item $tmp -ErrorAction SilentlyContinue
}
