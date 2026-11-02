/* The C API from C: b3.h compiles as C11 with every warning on, and each
 * call's digest of a few inputs is printed in hex, for tests/c.rs to check
 * against the Rust crate's. */
#include <stdio.h>
#include <string.h>

#include "b3.h"

static void print(const char *what, const uint8_t *bytes, size_t len) {
  printf("%s ", what);
  for (size_t i = 0; i < len; i++) printf("%02x", bytes[i]);
  printf("\n");
}

/* The stream test's state: four places cycled, each coming back with the
 * digest of message next_check, checked against b3_hash. */
struct stream_ctx {
  b3_digest *free_places[4];
  int free_count;
  size_t next_check;
  size_t slens[7];
  const uint8_t *input;
  const b3_mode *mode;
  int bad;
};

static void done(void *ctxp, b3_digest *d, size_t count) {
  struct stream_ctx *c = ctxp;
  for (size_t k = 0; k < count; k++) {
    size_t i = c->next_check++;
    b3_digest want = b3_hash(c->mode, c->input, c->slens[i % 7]);
    if (memcmp(want.bytes, d[k].bytes, 32) != 0) c->bad = 1;
    c->free_places[c->free_count++] = &d[k];
  }
}

static b3_digest c_places[4];

int main(void) {
  if (strcmp(b3_version(), B3_VERSION) != 0) return 2; /* the header and the library disagree */
  printf("hasher %zu %zu\n", sizeof(b3_hasher), _Alignof(b3_hasher));
  static B3_ALIGN64 uint8_t input[100032]; /* whole 64-byte blocks */
  for (size_t i = 0; i < sizeof input; i++) input[i] = (uint8_t)(i % 251);
  uint8_t key[32];
  memcpy(key, "whats the Elvish word for friend", 32);
  b3_mode modes[3] = {b3_mode_plain(), b3_mode_keyed(key), b3_mode_derive_key("BLAKE3 2019-12-27 16:29:52 test vectors context")};
  const char *names[3] = {"hash", "keyed_hash", "derive_key"};
  size_t lens[3] = {0, 1025, 100000};
  for (int m = 0; m < 3; m++) {
    for (int l = 0; l < 3; l++) {
      char what[64];
      snprintf(what, sizeof what, "%s/%zu", names[m], lens[l]);
      b3_digest d = b3_hash(&modes[m], input, lens[l]);
      print(what, d.bytes, 32);
      d = b3_hash_multithreaded(&modes[m], input, lens[l]);
      print(what, d.bytes, 32);
      static b3_hasher hasher;
      b3_hasher_new(&hasher, &modes[m]);
      b3_hasher_update(&hasher, input, lens[l] / 2);
      b3_hasher_update(&hasher, input + lens[l] / 2, lens[l] - lens[l] / 2);
      d = b3_hasher_finalize(&hasher);
      print(what, d.bytes, 32);
    }
  }
  /* A stream: 300 messages of lengths cycling through 7 values, through 4
   * places cycled by collect. Every digest is checked against b3_hash;
   * "stream ok" says so. */
  {
    struct stream_ctx c = {{&c_places[0], &c_places[1], &c_places[2], &c_places[3]}, 4, 0, {0, 1, 64, 1025, 4096, 65536, 100000}, input, &modes[0], 0};
    b3_stream *s = b3_stream_begin(&modes[0], done, &c);
    for (size_t i = 0; i < 300; i++) {
      while (c.free_count == 0) b3_stream_collect(s);
      b3_digest *place = c.free_places[--c.free_count];
      b3_stream_submit(s, input, c.slens[i % 7], place);
    }
    b3_stream_finish(s);
    if (c.next_check != 300 || c.bad) return 3;
    printf("stream ok\n");
  }
  return 0;
}

