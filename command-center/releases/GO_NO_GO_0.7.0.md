# 0.7.0 (Research Gateway): GO/NO-GO report

Date: 2026-10-20 · package `InfiniteAI-CommandCenter-0.7.0-windows.zip`,
SHA-256 in the `.sha256` file next to it (TODO, lead: after packaging) ·
branch `ccr-eb27a2af-8u7i39` · details in
[docs/RELEASE_NOTES_0.7.0.md](../docs/RELEASE_NOTES_0.7.0.md) and
[docs/RESEARCH_GATEWAY.md](../docs/RESEARCH_GATEWAY.md) · no Windows upgrade
guide (there is no upgrade to make while 0.7.0 is NO-GO)

> **Security notice, 2026-10-20: 0.7.0 is NO-GO, for installation and for any
> JARVIS import.** 0.7.0 is built on 0.6.1, which the security notice of the
> same day declared NO-GO, and it carries 0.6.1's JARVIS importer unchanged,
> so it has the same high-severity defect (reproduced three times
> independently on the exact published 0.6.1 code). The text of a JARVIS
> message that an admin withheld comes back when a **later** JARVIS backup
> is imported. JARVIS's compaction stores copies of messages under new
> identifiers, and the importer withholds only the identifier that was
> withheld, so a copy is imported with its text. Operators, admins, the
> Supervisor's history and recall, and newly promoted memory items can then
> read it (viewers cannot), and the verification commands still report
> success. Web research does not change this.
>
> - Do not install 0.7.0, 0.6.1 or 0.6.0, and do not import JARVIS data
>   with 0.7.0 or any 0.6.x release.
> - If you are still on 0.5.0, stay there. A corrected 0.6.2 is being
>   prepared, with a direct 0.5.0 -> 0.6.2 guide; it will be published only
>   after its security reviews pass. Web research will be offered for
>   installation only in a 0.7 release that also contains 0.6.2's fix,
>   tested on its own upgrades from 0.5.0 and from 0.6.2.
> - An installation of 0.6.0 or 0.6.1 that has imported nothing holds no
>   JARVIS data and is not exposed: leave it as it is and import nothing.

## Verdict

| Step | Verdict | Why |
|---|---|---|
| 1. Install 0.7.0 on your PC (from 0.5.0, or from any 0.6.x) | **NO-GO** | It carries 0.6.1's JARVIS importer unchanged (security notice). Stay on 0.5.0; do not install 0.6.0 or 0.6.1 on the way. |
| 2. Import JARVIS data with 0.7.0 (test copy or live installation) | **NO-GO** | The same defect: a withheld message's text comes back with a later import. |
| 3. `research.ps1 -Enable`, the real connectivity test, live research | **Not now** | They need 0.7.0 installed, and each needs your separate approval. |
| 4. Web research for installation | **After 0.6.2** | In a 0.7 release that contains 0.6.2's fix (its migration ordered before `0008_research`, or research renumbered), after the full test suite, upgrade tests from 0.5.0 and from 0.6.2, and its reviews. Going back from it: to 0.5.0, or to 0.6.2. |

Nothing in this list has been done. I have no access to your PC; the
installed 0.5.0, JARVIS and your live database are untouched.

## Tests (cloud sandbox)

TODO (lead): the package's tests, as listed in `RELEASE_NOTES_0.7.0.md`
section 7. They show that the package works as built (web research off after
install and upgrade, `.env` unchanged, going back to 0.5.0); they do not
change the verdict above.
