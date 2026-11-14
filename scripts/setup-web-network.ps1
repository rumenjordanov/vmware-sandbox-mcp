<#
.SYNOPSIS
    One-time host setup for network="web" sandboxes. Run in an elevated PowerShell.

.DESCRIPTION
    Sandboxes created with network="web" sit on a dedicated host-only VMware network.
    The only thing they may reach is the filtering proxy that vmware-sandbox-mcp runs
    on this computer's address on that network. This script makes Windows enforce that:

      * blocks every inbound TCP port from that network except the proxy port;
      * blocks inbound UDP and ICMP from that network;
      * allows the proxy port;
      * unbinds IPv6, file sharing and the Windows network client from that adapter.

    Every rule is limited to the one adapter you name, so no other network, VM or
    program is affected. Rules are in the firewall group "vmware-sandbox-mcp".

    Before running it, create the network in VMware Workstation (see README):
    Edit > Virtual Network Editor > Add Network > host-only, with a host virtual
    adapter and the local DHCP service enabled.

.EXAMPLE
    .\setup-web-network.ps1 -Vnet VMnet9

.EXAMPLE
    .\setup-web-network.ps1 -Vnet VMnet9 -Remove
#>
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^(?i)VMnet([2-7]|9|1[0-9])$')]   # never VMnet0 (bridged), VMnet1 or VMnet8 (NAT)
    [string]$Vnet,

    [ValidateRange(2, 65534)]
    [int]$ProxyPort = 3128,

    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$group = 'vmware-sandbox-mcp'
$alias = "VMware Network Adapter $Vnet"
$bindings = 'ms_tcpip6', 'ms_server', 'ms_msclient'

$adapter = Get-NetAdapter -Name $alias -ErrorAction SilentlyContinue
if (-not $adapter) {
    throw "No network adapter named '$alias'. Create the host-only network $Vnet in VMware's Virtual Network Editor first, with 'Connect a host virtual adapter to this network' ticked."
}
$alias = $adapter.Name

$natKey = "HKLM:\SOFTWARE\WOW6432Node\VMware, Inc.\VMnetLib\VMnetConfig\$($Vnet.ToLower())\NAT"
if ((Get-ItemProperty -Path $natKey -Name UseNAT -ErrorAction SilentlyContinue).UseNAT -eq 1) {
    throw "$Vnet is a NAT network. Sandboxes on it would bypass the proxy. Use a host-only network."
}

# Start clean so the script can be re-run (for example with another proxy port).
Get-NetFirewallRule -Group $group -ErrorAction SilentlyContinue | Remove-NetFirewallRule

if ($Remove) {
    foreach ($id in $bindings) {
        Enable-NetAdapterBinding -Name $alias -ComponentID $id -ErrorAction SilentlyContinue
    }
    Write-Host "Removed the vmware-sandbox-mcp firewall rules and restored the adapter bindings on '$alias'."
    Write-Host "Unset VMSANDBOX_WEB_VNET and VMSANDBOX_WEB_PROXY_HOST to switch network=""web"" off."
    return
}

$ip = (Get-NetIPAddress -InterfaceAlias $alias -AddressFamily IPv4 | Select-Object -First 1).IPAddress
if (-not $ip) { throw "'$alias' has no IPv4 address." }

foreach ($id in $bindings) {
    Disable-NetAdapterBinding -Name $alias -ComponentID $id -ErrorAction SilentlyContinue
}

$common = @{ Group = $group; Direction = 'Inbound'; Profile = 'Any'; InterfaceAlias = $alias }
$otherPorts = "1-$($ProxyPort - 1)", "$($ProxyPort + 1)-65535"

New-NetFirewallRule @common -Action Block -Protocol TCP -LocalPort $otherPorts `
    -DisplayName "vmware-sandbox-mcp: block TCP from sandboxes (except proxy)" | Out-Null
New-NetFirewallRule @common -Action Block -Protocol UDP `
    -DisplayName "vmware-sandbox-mcp: block UDP from sandboxes" | Out-Null
New-NetFirewallRule @common -Action Block -Protocol ICMPv4 `
    -DisplayName "vmware-sandbox-mcp: block ICMPv4 from sandboxes" | Out-Null
New-NetFirewallRule @common -Action Block -Protocol ICMPv6 `
    -DisplayName "vmware-sandbox-mcp: block ICMPv6 from sandboxes" | Out-Null
New-NetFirewallRule @common -Action Allow -Protocol TCP -LocalPort $ProxyPort -LocalAddress $ip `
    -DisplayName "vmware-sandbox-mcp: allow sandboxes to reach the proxy" | Out-Null

Write-Host ""
Write-Host "Done. Firewall rules created for '$alias':"
Get-NetFirewallRule -Group $group | Sort-Object DisplayName |
    ForEach-Object { Write-Host ("  [{0}] {1}" -f $_.Action, $_.DisplayName) }
Write-Host ""
Write-Host "Add these to the server's environment:"
Write-Host "  VMSANDBOX_WEB_VNET=$Vnet"
Write-Host "  VMSANDBOX_WEB_PROXY_HOST=$ip"
if ($ProxyPort -ne 3128) { Write-Host "  VMSANDBOX_WEB_PROXY_PORT=$ProxyPort" }
Write-Host ""
Write-Host "Then run 'vmware-sandbox-mcp doctor' to verify from inside a real sandbox."
