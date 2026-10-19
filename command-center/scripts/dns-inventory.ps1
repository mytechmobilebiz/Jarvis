# Read-only DNS inventory of a domain. It only asks DNS questions: it changes
# nothing in DNS, at the registrar, in CWP or on this PC (apart from writing the
# report file next to where you run it).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\dns-inventory.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\dns-inventory.ps1 -Domain example.com
#
# It asks a public resolver (1.1.1.1) and then the domain's own authoritative
# nameserver directly, so you can see what the world sees and what the source
# says. DNS cannot list every record of a zone; compare the result with the zone
# shown in CWP (DNS Functions > Edit DNS Zone; look, do not save).
param(
  [string]$Domain = "infinitemarketingai.com",
  [string]$Resolver = "1.1.1.1"
)

$report = Join-Path (Get-Location) ("dns-inventory-{0}-{1}.txt" -f $Domain, (Get-Date -Format "yyyyMMdd-HHmmss"))
$lines = New-Object System.Collections.Generic.List[string]
function Out-Line([string]$text) { Write-Host $text; $lines.Add($text) }

function Format-Record($r) {
  switch ($r.Type) {
    "A" { return $r.IPAddress }
    "AAAA" { return $r.IPAddress }
    "NS" { return $r.NameHost }
    "CNAME" { return $r.NameHost }
    "MX" { return "{0} {1}" -f $r.Preference, $r.NameExchange }
    "TXT" { return '"' + ($r.Strings -join "") + '"' }
    "SOA" { return "{0} {1} serial {2} refresh {3} expire {4} min-ttl {5}" -f $r.PrimaryServer, $r.NameAdministrator, $r.SerialNumber, $r.TimeToZoneRefresh, $r.TimeToExpiration, $r.DefaultTTL }
    "SRV" { return "{0} {1} {2} {3}" -f $r.Priority, $r.Weight, $r.Port, $r.NameTarget }
    "DS" { return "keytag {0} algorithm {1} digest-type {2}" -f $r.KeyTag, $r.Algorithm, $r.DigestType }
    "PTR" { return $r.NameHost }
    default { return ($r | Out-String).Trim() }
  }
}

function Ask([string]$name, [string]$type, [string]$server) {
  try {
    $answers = @(Resolve-DnsName -Name $name -Type $type -Server $server -DnsOnly -ErrorAction Stop | Where-Object { $_.Section -eq "Answer" })
    if (-not $answers) { Out-Line ("{0,-44} {1,-6} (no record)" -f $name, $type); return }
    foreach ($r in $answers) { Out-Line ("{0,-44} {1,-6} ttl {2,-6} {3}" -f $r.Name, $r.Type, $r.TTL, (Format-Record $r)) }
  } catch {
    Out-Line ("{0,-44} {1,-6} (none: {2})" -f $name, $type, $_.Exception.Message)
  }
}

$names = @(
  @($Domain, @("SOA", "NS", "A", "AAAA", "MX", "TXT", "DS")),
  @("www.$Domain", @("CNAME", "A", "AAAA")),
  @("mail.$Domain", @("CNAME", "A")),
  @("webmail.$Domain", @("CNAME", "A")),
  @("ftp.$Domain", @("CNAME", "A")),
  @("autodiscover.$Domain", @("CNAME", "A")),
  @("autoconfig.$Domain", @("CNAME", "A")),
  @("ns1.$Domain", @("A")),
  @("ns2.$Domain", @("A")),
  @("_dmarc.$Domain", @("TXT")),
  @("default._domainkey.$Domain", @("TXT")),
  @("_autodiscover._tcp.$Domain", @("SRV")),
  @("command.$Domain", @("CNAME", "A")),
  @("clients.$Domain", @("CNAME", "A")),
  @("api.$Domain", @("CNAME", "A")),
  @("hooks.$Domain", @("CNAME", "A"))
)

Out-Line "DNS inventory of $Domain, $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'), from $env:COMPUTERNAME"
Out-Line ""
Out-Line "=== As the public resolver $Resolver sees it"
foreach ($entry in $names) { foreach ($type in $entry[1]) { Ask $entry[0] $type $Resolver } }

# Reverse DNS of the website's address: mail servers check that this name resolves back to
# the same address, so if it is a name under this domain it must be kept when DNS moves.
Out-Line ""
Out-Line "=== Reverse DNS (PTR) of the domain's address, and that name's own address"
$apexIps = @(Resolve-DnsName -Name $Domain -Type A -Server $Resolver -DnsOnly -ErrorAction SilentlyContinue | Where-Object { $_.Section -eq "Answer" -and $_.Type -eq "A" } | ForEach-Object { $_.IPAddress })
foreach ($ip in $apexIps) {
  $parts = $ip.Split(".")
  $reverse = "{0}.{1}.{2}.{3}.in-addr.arpa" -f $parts[3], $parts[2], $parts[1], $parts[0]
  Ask $reverse "PTR" $Resolver
  $ptr = @(Resolve-DnsName -Name $reverse -Type PTR -Server $Resolver -DnsOnly -ErrorAction SilentlyContinue | Where-Object { $_.Section -eq "Answer" } | ForEach-Object { $_.NameHost })
  foreach ($name in $ptr) { Ask $name "A" $Resolver }
}

# A name that cannot exist: if it answers, the zone has a wildcard (*) record, which must be copied too.
Out-Line ""
Out-Line "=== Wildcard check"
Ask ("wildcard-check-{0}.{1}" -f (Get-Random -Minimum 100000 -Maximum 999999), $Domain) "A" $Resolver

$authoritative = @(Resolve-DnsName -Name $Domain -Type NS -Server $Resolver -DnsOnly -ErrorAction SilentlyContinue | Where-Object { $_.Section -eq "Answer" } | ForEach-Object { $_.NameHost })
Out-Line ""
if ($authoritative) {
  $primary = $authoritative[0]
  Out-Line "=== Asked directly at the authoritative nameserver $primary"
  foreach ($entry in $names) { foreach ($type in $entry[1]) { if ($type -ne "DS") { Ask $entry[0] $type $primary } } }
} else {
  Out-Line "=== No NS answer, so the authoritative nameserver could not be asked directly"
}

Out-Line ""
Out-Line "Notes"
Out-Line "- NS: if these end in ns.cloudflare.com the zone is already on Cloudflare; otherwise it is hosted elsewhere (e.g. CWP on the VPS, or the registrar)."
Out-Line "- DS: a DS record means DNSSEC is switched on at the registrar; it must be handled before any nameserver change."
Out-Line "- DKIM may use a selector other than 'default'; the CWP zone editor shows the real one."
Out-Line "- PTR: if the reverse name is under this domain (e.g. a server hostname), its A record must be kept, or mail may be rejected."
Out-Line "- Wildcard: an answer there means a '*' record exists."
Out-Line "- Comparing a report made with -Resolver <a Cloudflare nameserver>: SOA, NS and DS lines legitimately differ; all others must match."
Out-Line "- TXT records hold public data (SPF, DKIM public keys, verification tokens); this report contains no secrets."
$lines | Set-Content -Path $report -Encoding ASCII
Write-Host ""
Write-Host "Saved: $report" -ForegroundColor Green
