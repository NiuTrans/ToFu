/* Canonical C declarations for native_html_projection_abi_v1.json.
 * Call and lifetime semantics remain in that versioned contract. */
#ifndef TOFU_HTML_PROJECTION_ABI_V1_H
#define TOFU_HTML_PROJECTION_ABI_V1_H
#include <stddef.h>
#include <stdint.h>

typedef uint32_t (*tofu_html_sink)(void *context, const uint8_t *data, size_t length);
uint32_t tofu_html_projection_abi_version(void);
uint32_t tofu_html_projection_profile(tofu_html_sink sink, void *context);
uint32_t tofu_html_projection_project(const uint8_t *input, size_t length, size_t projection_budget, tofu_html_sink sink, void *context);

#endif
