/* bootmod3 CustomROM map slots on the DME, ported from cauliflowermossgrape/idrive7-apps
 * apps/live_adjustments/bm3_protocol.js (MIT, third-party/idrive7-apps-LICENSE), itself from BM3 Live by ezl0l.
 * This project is independent of and unaffiliated with bootmod3.
 *
 * Map switching is not a BMW job: bootmod3's code in the DME handles it. The app reads the CustomROM marker from DME
 * memory and goes on only when it names a ROM in bm3_profiles.h. The map slot is then written to the profile's
 * address with bootmod3's memory-write command and read back. Everything here is pure: building requests and checking
 * replies. The I/O is in collector.c (BM3_MAPS_ONLY). */
#ifndef BM3_H
#define BM3_H

#include <stddef.h>
#include <stdint.h>

enum {
    BM3_DME = 0x12,
    BM3_MARKER_SIZE = 0xFF,
    BM3_SLOTS = 4,
    BM3_MARKER_ADDRESS = 0x807FFE50u /* upstream's default; set per car in ENA_DATA_DIR/bm3-marker-address */
};

typedef struct {
    const char *rom;
    uint32_t map_address;    /* the active slot (0..3), then an activation flag */
    uint32_t burble_address; /* not used yet */
    uint32_t timing_address; /* not used yet */
    unsigned factor;         /* not used yet */
    unsigned write_service;  /* 0x3D WriteMemoryByAddress, or 0x3E as bootmod3 uses it on some ROMs */
} bm3_profile;

/* The profile for a ROM, or NULL. */
const bm3_profile *bm3_profile_for(const char *rom);

/* The ROM key named in the marker's 255 bytes, or NULL with *why set. Three layouts, as upstream:
 * a binary block (BM3_, 64 bytes, ~|~, underscores, #ROM + 5 build characters, NUL),
 * the S58 one (BM3_, binary, the only # at byte 101, ROM (9) + build (5) + _ + chassis..., NUL),
 * and the older text one (BM3_R somewhere, #ROM). A ROM must match exactly one profile. */
const bm3_profile *bm3_marker_profile(const unsigned char *marker, size_t size, const char **why);

/* Requests, as UDS bytes for the DME. Each returns its length. */
size_t bm3_clear_dynamic_did(unsigned char *out);                     /* 2C 03 F3 01 */
size_t bm3_define_marker_did(unsigned char *out, uint32_t address);  /* 2C 02 F3 01 24 AAAAAAAA 00 FF */
size_t bm3_read_marker_did(unsigned char *out);                       /* 22 F3 01 */
size_t bm3_read_memory(unsigned char *out, uint32_t address, unsigned length); /* 23 14 AAAAAAAA LL */
/* Write: SID 14 AAAAAAAA LL data. 0 if the profile's service is unknown or the data doesn't fit. */
size_t bm3_write_memory(unsigned char *out, size_t cap, const bm3_profile *profile, uint32_t address,
                        const unsigned char *data, unsigned length);

/* The positive reply's first bytes for a request built above (the SID + 0x40, then what the DME echoes). */
size_t bm3_reply_prefix(const unsigned char *request, unsigned char *prefix);

/* Parses a one-byte slot read (63 SS). Returns 0..3, or -1. */
int bm3_slot_reply(const unsigned char *reply, size_t size);

#endif
