/**
 * Windows code signing: NOT IN THIS RELEASE, and deliberately so.
 *
 * JARVIS has no Windows code-signing certificate yet (Certum OV is being
 * purchased). Until it arrives the Windows artifacts ship UNSIGNED, with the
 * SmartScreen caveat carried in the support docs — and that has to be a
 * DECISION in the build config, not an accident of what electron-builder finds
 * on the machine it runs on. CI logs showed it invoking signtool.exe for every
 * executable in the package, including the vendored Python and the two voice
 * helpers, with no certificate configured anywhere in this repo.
 *
 * A broken or self-signed signature is worse than none: SmartScreen and most
 * AV engines treat an untrusted signer as a stronger signal than an unsigned
 * binary, and signing also REWRITES each file — which would break the payload
 * digest chains that prove the engine and voice assets are the ones we built
 * and tested.
 *
 * electron-builder calls this hook INSTEAD of signtool. Doing nothing here is
 * the whole point.
 *
 * When the certificate lands: delete this hook, configure the certificate the
 * normal way, and implement the Windows branch of setup/src/code-signature.ts
 * FIRST — otherwise the re-signed voice helpers will fail their hash check and
 * ship mute, exactly as they did on macOS before the Apple-chain verifier.
 */
exports.default = async function windowsSigningIsIntentionallyDisabled() {
  // Intentionally empty.
};
