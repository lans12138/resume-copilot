[CmdletBinding()]
param(
    [Parameter()]
    [ValidateSet('Validate', 'ConfigureNodePath', 'EnableWsl', 'EnableWslFeatures', 'InstallWslPackage', 'InstallUbuntu', 'InstallDocker')]
    [string] $Action = 'Validate'
)

$ErrorActionPreference = 'Stop'

$developRoot = [IO.Path]::GetFullPath('D:\develop')
$nodeRoot = [IO.Path]::GetFullPath('D:\develop\node-v22.23.2-win-x64')
$wslInstaller = [IO.Path]::GetFullPath('D:\develop\installers\wsl.2.7.12.0.x64.msi')
$ubuntuInstaller = [IO.Path]::GetFullPath('D:\develop\installers\ubuntu-24.04.4-wsl-amd64.wsl')
$ubuntuInstallerSha256 = '9b2f7730dc68227dd04a9f3e5eab86ad85caf556b8606ad94f1f29ff5c4fd3f5'
$ubuntuRoot = [IO.Path]::GetFullPath('D:\develop\wsl\Ubuntu-24.04')
$dockerInstaller = [IO.Path]::GetFullPath('D:\develop\installers\Docker Desktop Installer.exe')
$dockerInstallRoot = [IO.Path]::GetFullPath('D:\develop\Docker')
$dockerDataRoot = [IO.Path]::GetFullPath('D:\develop\DockerData\wsl')

function Assert-PathUnderDevelop {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Path
    )

    $resolvedCandidate = [IO.Path]::GetFullPath($Path)
    $requiredPrefix = $developRoot + [IO.Path]::DirectorySeparatorChar
    if (-not $resolvedCandidate.StartsWith($requiredPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Target path is outside D:\develop: $resolvedCandidate"
    }
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-Administrator {
    if (-not (Test-IsAdministrator)) {
        throw 'This action requires an administrator PowerShell session.'
    }
}

function Get-CommandVersion {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Command,

        [Parameter()]
        [string[]] $Arguments = @('--version')
    )

    $commandInfo = Get-Command $Command -ErrorAction SilentlyContinue
    if (-not $commandInfo) {
        return 'NOT_FOUND'
    }

    try {
        return ((& $Command @Arguments 2>&1) -join ' ').Trim()
    } catch {
        return "ERROR: $($_.Exception.Message)"
    }
}

function Invoke-Validation {
    $nodeExecutable = Join-Path $nodeRoot 'node.exe'
    $nodeVersion = if (Test-Path -LiteralPath $nodeExecutable) {
        (& $nodeExecutable --version).Trim()
    } else {
        'NOT_FOUND'
    }

    $dockerSignatureStatus = 'NOT_FOUND'
    $dockerSigner = $null
    if (Test-Path -LiteralPath $dockerInstaller) {
        try {
            $signature = Get-AuthenticodeSignature -LiteralPath $dockerInstaller
            $dockerSignatureStatus = $signature.Status.ToString()
            $dockerSigner = $signature.SignerCertificate.Subject
        } catch {
            $dockerSignatureStatus = "ERROR: $($_.Exception.Message)"
        }
    }

    [pscustomobject]@{
        IsAdministrator = Test-IsAdministrator
        DevelopRoot = $developRoot
        NodeTarget = $nodeRoot
        NodeTargetVersion = $nodeVersion
        NodeOnPath = Get-CommandVersion -Command 'node'
        NpmOnPath = Get-CommandVersion -Command 'npm'
        Wsl = Get-CommandVersion -Command 'wsl' -Arguments @('--version')
        WslInstaller = $wslInstaller
        UbuntuInstaller = $ubuntuInstaller
        UbuntuRoot = $ubuntuRoot
        Docker = Get-CommandVersion -Command 'docker'
        DockerInstaller = $dockerInstaller
        DockerSignature = $dockerSignatureStatus
        DockerSigner = $dockerSigner
    }
}

function Invoke-ConfigureNodePath {
    Assert-Administrator
    Assert-PathUnderDevelop -Path $nodeRoot

    $nodeExecutable = Join-Path $nodeRoot 'node.exe'
    if (-not (Test-Path -LiteralPath $nodeExecutable)) {
        throw "Node executable not found: $nodeExecutable"
    }

    $nodeVersion = (& $nodeExecutable --version).Trim()
    if ($nodeVersion -ne 'v22.23.2') {
        throw "Unexpected Node target version: $nodeVersion"
    }

    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $pathEntries = @(
        $machinePath.Split(';', [StringSplitOptions]::RemoveEmptyEntries) |
            ForEach-Object { $_.Trim() } |
            Where-Object { -not $_.Equals($nodeRoot, [StringComparison]::OrdinalIgnoreCase) }
    )
    $newMachinePath = (@($nodeRoot) + $pathEntries) -join ';'
    [Environment]::SetEnvironmentVariable('Path', $newMachinePath, 'Machine')

    [pscustomobject]@{
        Action = 'ConfigureNodePath'
        NodeRoot = $nodeRoot
        NodeVersion = $nodeVersion
        RestartTerminalRequired = $true
    }
}

function Invoke-EnableWsl {
    Assert-Administrator

    $wslExecutable = Join-Path $env:SystemRoot 'System32\wsl.exe'
    if (-not (Test-Path -LiteralPath $wslExecutable)) {
        throw "WSL executable not found: $wslExecutable"
    }

    & $wslExecutable --install --no-distribution --web-download
    $webInstallExitCode = $LASTEXITCODE
    $fallbackUsed = $false
    if ($webInstallExitCode -notin @(0, 3010)) {
        # Some Windows 10 installations cannot register the Store/MSI WSL package.
        # Enable the built-in Windows components first; the WSL package can be
        # updated after the required restart.
        $fallbackUsed = $true
        & $wslExecutable --install --inbox --no-distribution
        $installExitCode = $LASTEXITCODE
    } else {
        $installExitCode = $webInstallExitCode
    }

    $featureFallbackUsed = $false
    if ($installExitCode -notin @(0, 3010)) {
        $featureFallbackUsed = $true
        $featureResult = Invoke-EnableWslFeatures
        $installExitCode = $featureResult.ExitCode
    }

    [pscustomobject]@{
        Action = 'EnableWsl'
        ExitCode = $installExitCode
        WebInstallExitCode = $webInstallExitCode
        InboxFallbackUsed = $fallbackUsed
        FeatureFallbackUsed = $featureFallbackUsed
        RestartRequired = $true
        DistributionInstalled = $false
    }
}

function Invoke-EnableWslFeatures {
    Assert-Administrator

    $dismExecutable = Join-Path $env:SystemRoot 'System32\dism.exe'
    if (-not (Test-Path -LiteralPath $dismExecutable)) {
        throw "DISM executable not found: $dismExecutable"
    }

    $features = @(
        'VirtualMachinePlatform',
        'Microsoft-Windows-Subsystem-Linux'
    )
    $featureResults = @()
    foreach ($feature in $features) {
        & $dismExecutable /Online /Enable-Feature "/FeatureName:$feature" /All /NoRestart
        $featureExitCode = $LASTEXITCODE
        if ($featureExitCode -notin @(0, 3010)) {
            throw "Failed to enable Windows feature $feature; exit code: $featureExitCode"
        }
        $featureResults += [pscustomobject]@{
            FeatureName = $feature
            ExitCode = $featureExitCode
        }
    }

    [pscustomobject]@{
        Action = 'EnableWslFeatures'
        ExitCode = 0
        Features = $featureResults
        RestartRequired = $true
    }
}

function Invoke-InstallWslPackage {
    Assert-Administrator
    Assert-PathUnderDevelop -Path $wslInstaller

    if (-not (Test-Path -LiteralPath $wslInstaller)) {
        throw "WSL installer not found: $wslInstaller"
    }

    $signature = Get-AuthenticodeSignature -LiteralPath $wslInstaller
    if ($signature.Status -ne 'Valid') {
        throw "WSL installer signature is not valid: $($signature.Status)"
    }
    if ($signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') {
        throw "Unexpected WSL installer signer: $($signature.SignerCertificate.Subject)"
    }

    $msiArguments = @(
        '/i',
        ('"{0}"' -f $wslInstaller),
        '/qn',
        '/norestart'
    )
    $startProcessArguments = @{
        FilePath = (Join-Path $env:SystemRoot 'System32\msiexec.exe')
        ArgumentList = $msiArguments
        Wait = $true
        PassThru = $true
        WindowStyle = 'Hidden'
    }
    $installerProcess = Start-Process @startProcessArguments
    if ($installerProcess.ExitCode -notin @(0, 3010)) {
        throw "WSL package installation failed with exit code: $($installerProcess.ExitCode)"
    }

    [pscustomobject]@{
        Action = 'InstallWslPackage'
        Installer = $wslInstaller
        InstallerSigner = $signature.SignerCertificate.Subject
        ExitCode = $installerProcess.ExitCode
        RestartRequired = $true
    }
}

function Invoke-InstallUbuntu {
    Assert-PathUnderDevelop -Path $ubuntuRoot

    $wslExecutable = Join-Path $env:SystemRoot 'System32\wsl.exe'
    if (-not (Test-Path -LiteralPath $wslExecutable)) {
        throw "WSL executable not found: $wslExecutable"
    }

    $ubuntuParent = Split-Path -Parent $ubuntuRoot
    New-Item -ItemType Directory -Path $ubuntuParent -Force | Out-Null

    $installSource = 'web-download'
    if (Test-Path -LiteralPath $ubuntuInstaller) {
        $actualHash = (Get-FileHash -LiteralPath $ubuntuInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $ubuntuInstallerSha256) {
            throw "Ubuntu installer checksum mismatch: $actualHash"
        }
        $installSource = $ubuntuInstaller
        & $wslExecutable --install --from-file $ubuntuInstaller --location $ubuntuRoot --name Ubuntu-24.04 --no-launch
    } else {
        & $wslExecutable --install Ubuntu-24.04 --location $ubuntuRoot --no-launch --web-download
    }
    $installExitCode = $LASTEXITCODE
    if ($installExitCode -ne 0) {
        throw "Ubuntu installation failed with exit code: $installExitCode"
    }

    $distributionFilesReady = Test-Path -LiteralPath $ubuntuRoot

    [pscustomobject]@{
        Action = 'InstallUbuntu'
        Distribution = 'Ubuntu-24.04'
        Source = $installSource
        InstallRoot = $ubuntuRoot
        Installed = $distributionFilesReady
        PendingRestart = -not $distributionFilesReady
        LaunchRequired = $distributionFilesReady
    }
}

function Invoke-InstallDocker {
    Assert-Administrator
    foreach ($targetPath in @($dockerInstaller, $dockerInstallRoot, $dockerDataRoot)) {
        Assert-PathUnderDevelop -Path $targetPath
    }

    if (-not (Test-Path -LiteralPath $dockerInstaller)) {
        throw "Docker Desktop installer not found: $dockerInstaller"
    }

    $signature = Get-AuthenticodeSignature -LiteralPath $dockerInstaller
    if ($signature.Status -ne 'Valid') {
        throw "Docker installer signature is not valid: $($signature.Status)"
    }
    if ($signature.SignerCertificate.Subject -notmatch 'Docker') {
        throw "Unexpected Docker installer signer: $($signature.SignerCertificate.Subject)"
    }

    New-Item -ItemType Directory -Path $dockerInstallRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $dockerDataRoot -Force | Out-Null

    $installerArguments = @(
        'install',
        "--installation-dir=$dockerInstallRoot",
        '--backend=wsl-2',
        "--wsl-default-data-root=$dockerDataRoot",
        '--no-windows-containers'
    )
    $startProcessArguments = @{
        FilePath = $dockerInstaller
        ArgumentList = $installerArguments
        Wait = $true
        PassThru = $true
        WindowStyle = 'Hidden'
    }
    $installerProcess = Start-Process @startProcessArguments

    if ($installerProcess.ExitCode -ne 0) {
        throw "Docker Desktop installation failed with exit code: $($installerProcess.ExitCode)"
    }

    [pscustomobject]@{
        Action = 'InstallDocker'
        InstallRoot = $dockerInstallRoot
        WslDataRoot = $dockerDataRoot
        InstallerSigner = $signature.SignerCertificate.Subject
        LaunchRequired = $true
    }
}

foreach ($configuredPath in @($nodeRoot, $wslInstaller, $ubuntuInstaller, $ubuntuRoot, $dockerInstaller, $dockerInstallRoot, $dockerDataRoot)) {
    Assert-PathUnderDevelop -Path $configuredPath
}

switch ($Action) {
    'Validate' { Invoke-Validation }
    'ConfigureNodePath' { Invoke-ConfigureNodePath }
    'EnableWsl' { Invoke-EnableWsl }
    'EnableWslFeatures' { Invoke-EnableWslFeatures }
    'InstallWslPackage' { Invoke-InstallWslPackage }
    'InstallUbuntu' { Invoke-InstallUbuntu }
    'InstallDocker' { Invoke-InstallDocker }
}
