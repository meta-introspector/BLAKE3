# BLAKE3 servil for C and C++

`libb3_servil` exposes `blake3-servil`'s one-shot, incremental and
streaming hashing through [`b3.h`](b3.h). The header documents each call's
contract. It works with C11 and C++11.

## Build and install

Build tools: Rust, a C compiler,
[cargo-c 0.10.26+cargo-0.100.0](https://github.com/lu-zero/cargo-c/releases/tag/v0.10.26),
and `pkg-config`, available on `PATH`.

On Linux or macOS, from the repository root:

```sh
cargo cinstall --release --manifest-path capi/Cargo.toml \
    --prefix="$HOME/.local" --libdir=lib
export PKG_CONFIG_PATH="$HOME/.local/lib/pkgconfig"
```

This installs `include/b3.h`, the static and shared libraries in `lib/`,
and `lib/pkgconfig/b3_servil.pc`. The shared library has a versioned
library name. `--destdir=STAGING_DIRECTORY` stages these files for packaging;
the installed paths still use `--prefix`.

## Hash a message

Save this as `example.c`:

```c
#include <b3.h>
#include <stdio.h>

int main(void) {
    b3_mode mode = b3_mode_plain();
    b3_digest digest = b3_hash(&mode, "hello", 5);
    for (unsigned i = 0; i < 32; ++i) printf("%02x", digest.bytes[i]);
    puts("");
    return 0;
}
```

Compile and run against the installation above:

```sh
pkg-config --cflags --libs b3_servil > b3-flags
cc example.c @b3-flags \
    -Xlinker -rpath -Xlinker "$HOME/.local/lib" -o example
./example
```

For an installation containing only the static library, use
`cargo cinstall --library-type=staticlib` with the same installation options,
and generate `b3-flags` with `pkg-config --static --cflags --libs b3_servil`.
The `--static` flag supplies the system libraries Rust's static library needs.

## Choose a call

- `b3_hash`: one message already in memory.
- `b3_hasher_new`, `b3_hasher_update`, `b3_hasher_finalize`: one message
  arriving in pieces (`b3_hasher_update_multithreaded` for a large piece;
  `b3_hasher_finalize_xof` for output of any length).
- `b3_stream_begin`, `b3_stream_submit`, `b3_stream_collect`,
  `b3_stream_finish`: a continuing series of
  messages, with results delivered to a callback.
  [`tests/check.c`](tests/check.c) includes a complete example.

`B3_VERSION` identifies the header; `b3_version()` identifies the linked
library. Both carry the release's full version string.
