/* libb3_servil: the C API of blake3-servil.
 *
 * Hashing entry points initialize the library automatically. Initialization
 * runs a startup self-test, which stops the program if this build or CPU
 * computes wrong digests. Later initialization calls check the completed
 * state. Calls that use other CPU cores (`_multithreaded`) start the
 * library's threads at their first use; they last for the process.
 *
 * Memory: pointers cover live, correctly aligned storage of the stated
 * sizes, readable for inputs and writable for outputs. Output storage may
 * hold anything, uninitialized too: every call writes all of it. Read-only ranges
 * may overlap; a written range is disjoint from every other range a call
 * uses. Inputs stay unchanged, and outputs are not accessed concurrently
 * by the caller. The stream's longer access/lifetime rules are below.
 * Simple guards reject null callbacks, misaligned handles, unrepresentable
 * sized ranges, and immediate hasher data overlaps before borrowing
 * them. Detected invalid arguments stop the program with a message.
 * These checks do not establish allocation ownership or lifetime and do
 * not track overlaps between outstanding stream submissions. The remaining
 * memory rules are caller obligations; violations may be undefined behavior.
 *
 * Threads: any call may be made from any thread. A b3_mode is a value and
 * may be shared and read by any number of threads at once. Calls using the
 * same b3_hasher or b3_stream handle are serialized by the caller. After
 * fork() from a process with other threads, the child must exec before
 * using this API; no recovery of inherited library state is promised. (A
 * multithreaded call or stream in a child forked after the pool started
 * stops the program with a message.)
 *
 * Link with -lb3_servil (libb3_servil.a, or libb3_servil.so /
 * libb3_servil.dylib). Build and install with `cargo cinstall --release`
 * in capi/; `pkg-config --cflags --libs b3_servil` gives the compiler flags.
 * The shared library has a versioned soname. C11, or C++11.
 * B3_VERSION names the header's version; b3_version() the library's, so a
 * program can check that the two agree.
 */
#define B3_VERSION "0.8.0+b3c5efc7c975d903762dea809066f7c0a0603861"
#ifndef B3_H
#define B3_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* The library's version, the same text as B3_VERSION in the header it was
 * built with. */
const char *b3_version(void);

/* A digest: 32 bytes. */
typedef struct {
  uint8_t bytes[32];
} b3_digest;

/* A mode: which of BLAKE3's three functions a call computes. Make one with
 * b3_mode_plain, b3_mode_keyed, or b3_mode_derive_key, and pass it to as many calls as
 * you like; its fields are the library's. */
typedef struct {
  uint8_t kind;
  uint8_t key[32];
} b3_mode;

/* The plain hash function. */
b3_mode b3_mode_plain(void);

/* The keyed hash function (a MAC) with this 32-byte key. */
b3_mode b3_mode_keyed(const uint8_t key[32]);

/* Key derivation (a KDF) with this context: a NUL-terminated UTF-8 string,
 * hardcoded, globally unique, and application-specific, such as
 * "example.com 2026-10-16 session keys". The context is hashed here, once;
 * the calls' inputs are then the key material. */
b3_mode b3_mode_derive_key(const char *context);

/* The digest of `len` bytes at `input` (`input` may be NULL when `len` is
 * 0), on the calling thread. */
b3_digest b3_hash(const b3_mode *mode, const void *input, size_t len);

/* b3_hash, using other CPU cores for larger inputs. Threading overhead can
 * outweigh parallel work near the split threshold; b3_hash stays serial. */
b3_digest b3_hash_multithreaded(const b3_mode *mode, const void *input, size_t len);

/* One message fed in pieces. A b3_hasher is your memory (18 KiB; on the
 * stack, in a struct, or allocated). It needs no library destructor; the
 * caller frees any heap storage it supplied. b3_hasher_new starts a
 * message, and starts one over. Heap storage must also be 64-byte aligned;
 * ordinary malloc does not guarantee this over-alignment. Use a
 * platform-appropriate aligned allocator and its matching deallocator.
 * Use the header matching the linked library. */
#define B3_HASHER_SIZE 18432
#ifdef __cplusplus
#define B3_ALIGN64 alignas(64)
#else
#define B3_ALIGN64 _Alignas(64)
#endif
typedef struct {
  B3_ALIGN64 uint8_t opaque[B3_HASHER_SIZE];
} b3_hasher;

void b3_hasher_new(b3_hasher *hasher, const b3_mode *mode);

/* Add the message's next `len` bytes. */
void b3_hasher_update(b3_hasher *hasher, const void *input, size_t len);

/* b3_hasher_update, using other CPU cores where that is faster. */
void b3_hasher_update_multithreaded(b3_hasher *hasher, const void *input, size_t len);

/* The digest of the bytes so far; the hasher can take more. */
b3_digest b3_hasher_finalize(const b3_hasher *hasher);

/* Extended output: `len` bytes of the output from byte `offset` on, into
 * `out`. Its first 32 bytes are the digest. */
void b3_hasher_finalize_xof(const b3_hasher *hasher, uint64_t offset, void *out, size_t len);

/* A stream: messages one after another, hashed on other CPU cores while
 * your program reads or makes the next ones; the fastest way to hash many
 * things. Begin one, submit each message with the place its digest goes
 * (your memory, which stays yours), and finish. Each place comes back to
 * `done` once its digests are in it, in submission order, on the thread
 * that calls the stream: during a submit when the stream is full (it holds
 * up to 1024 messages in flight), during b3_stream_collect, and during
 * b3_stream_finish, which hands back every place and ends the stream.
 * A submitted message starts at a 64-byte boundary, its storage whole
 * 64-byte blocks (aligned_alloc(64, n) with n a multiple of 64; the bytes
 * past its end are ignored); a misplaced message stops the program.
 * Every message and place submitted lives until b3_stream_finish. Inputs
 * stay unchanged until finish. An output place is disjoint from submitted
 * inputs and all other outstanding places. After submission, do not
 * access it until done receives it. The callback may read/write those places. Reuse a returned
 * place only after the callback and the library call invoking it return.
 * done is non-null, returns normally (no C++ exception or longjmp through
 * library frames), and never calls back into the same stream or finishes
 * it. After b3_stream_finish returns, the stream handle is invalid.
 * A message stays unhashed in a task still open until the task fills or you
 * call collect or finish, so call one of them when you need a digest soon.
 * `ctx` goes to `done` unread. */
typedef struct b3_stream b3_stream;
typedef void (*b3_done)(void *ctx, b3_digest *places, size_t count);

b3_stream *b3_stream_begin(const b3_mode *mode, b3_done done, void *ctx);
void b3_stream_submit(b3_stream *stream, const void *input, size_t len, b3_digest *out);
void b3_stream_collect(b3_stream *stream);
void b3_stream_finish(b3_stream *stream);

#ifdef __cplusplus
}
#endif

#endif /* B3_H */
