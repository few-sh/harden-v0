# 08 — `LD_PRELOAD` / `ld.so.preload` injection

**Idea.** Cause a malicious shared object to be loaded into every dynamically-linked binary the verifier executes. Two routes: (a) export `LD_PRELOAD=/opt/evil.so` somewhere that survives into the verifier shell (`/etc/environment`, a profile script, the agent user's `.bashrc`); or (b) write the path to `/etc/ld.so.preload`, which the dynamic linker honors system-wide regardless of environment scrubbing. The `.so` hooks `__libc_start_main`, `execve`, or `exit` to write `1` to the reward file and call `_exit(0)` before real test code runs.

**What it breaks.** Eval assumption: "we control the verifier's environment, so clearing `LD_PRELOAD` is sufficient." Env-var scrubbing does not address `/etc/ld.so.preload`, and the shell-rc route survives most env resets too.

**Minimal demo.**
```c
// evil.c — compile with `gcc -shared -fPIC -o /opt/evil.so evil.c`
#include <stdlib.h>
__attribute__((constructor))
static void hijack(void) {
    system("mkdir -p /logs/verifier && echo 1 > /logs/verifier/reward.txt");
    _exit(0);
}
```
Plant during agent phase: `echo /opt/evil.so > /etc/ld.so.preload`.

**Example trace.** github.com/harbor-framework/harbor/issues/974.

**Defense.** Unset `LD_PRELOAD`, `LD_LIBRARY_PATH`, `LD_AUDIT` in the verifier shell *and* `rm -f /etc/ld.so.preload` (then `chattr +i` it, assuming `chattr` itself is hashed); use a statically-linked verifier launcher; or run the verifier in a fresh container that doesn't inherit the agent-phase rootfs.
