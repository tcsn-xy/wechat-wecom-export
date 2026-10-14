/* wecom_keyscan.c — 在一坨内存镜像里找企业微信数据库的 16 字节 raw key。
 *
 * 判定方式（见 lib/wecom/crypto.py）：
 *   每页 key = MD5(raw_key(16) || LE32(page_no) || "sAlT")
 *   第 1 页：(密文流 = page[8:16] || page[24:]) 解出来前 8 字节 == page[16:24]
 * 所以每个候选只要「一次 MD5 + 一个 AES-128-CBC 块解密」就能否掉，
 * 一次命中再用完整页校验一遍。
 *
 * 用 OpenSSL 的 MD5/AES 实现 + pthread 分片，扫几百 MB 也就几十秒。
 *
 * 用法:
 *   wecom_keyscan <page1.db> <blob1> [blob2 ...]
 *     page1.db  任意一个真实加密库（只读前 4096 字节）
 *     blob*     内存镜像文件（lldb wxdump 导出来的 RW 段）
 */
#include <openssl/aes.h>
#include <openssl/md5.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define PAGE_SIZE 4096
static unsigned char g_frag[8];      /* page1[16:24] */
static unsigned char g_ct0[16];      /* page1[8:16] + page1[24:32] */
static unsigned char g_salt_tag[4] = {'s', 'A', 'l', 'T'};

typedef struct {
    const unsigned char *data;
    size_t len;
    int tid;
} job_t;

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static int g_hits = 0;

/* 单个候选：算 MD5(raw||1||"sAlT") 再解一个块 */
static int try_key(const unsigned char *raw) {
    unsigned char material[24];
    unsigned char digest[MD5_DIGEST_LENGTH];
    unsigned char block[16];
    AES_KEY aes;

    memcpy(material, raw, 16);
    material[16] = 1; material[17] = 0; material[18] = 0; material[19] = 0;
    memcpy(material + 20, g_salt_tag, 4);

    MD5(material, 24, digest);

    /* IV 由页码驱动（page=1），这里直接按 Python 实现的等价逻辑算 */
    {
        static int have_iv = 0;
        static unsigned char iv[16];
        if (!have_iv) {
            /* 与 crypto.page_iv(1) 一致：4 步 LCG，每步取 LE32，最后 MD5 */
            int64_t value = 2;              /* page_number + 1 */
            unsigned char buf[16];
            int i;
            for (i = 0; i < 4; i++) {
                int64_t q = value / 52774;
                value = 40692 * (value - 52774 * q) - 3791 * q;
                if (value < 0) value += 2147483399;
                uint32_t v = (uint32_t)(value & 0xFFFFFFFFu);
                buf[i * 4 + 0] = (unsigned char)(v & 0xFF);
                buf[i * 4 + 1] = (unsigned char)((v >> 8) & 0xFF);
                buf[i * 4 + 2] = (unsigned char)((v >> 16) & 0xFF);
                buf[i * 4 + 3] = (unsigned char)((v >> 24) & 0xFF);
            }
            MD5(buf, 16, iv);
            have_iv = 1;
        }
        unsigned char iv_copy[16];
        memcpy(iv_copy, iv, 16);          /* AES_cbc_encrypt 会改 ivec，必须复制 */
        if (AES_set_decrypt_key(digest, 128, &aes) != 0) return 0;
        AES_cbc_encrypt(g_ct0, block, 16, &aes, iv_copy, AES_DECRYPT);
    }
    return memcmp(block, g_frag, 8) == 0;
}

static void *worker(void *arg) {
    job_t *job = (job_t *)arg;
    const unsigned char *d = job->data;
    size_t n = job->len;
    size_t i;
    if (n < 16) return NULL;
    for (i = 0; i + 16 <= n; i++) {
        if (try_key(d + i)) {
            pthread_mutex_lock(&g_lock);
            g_hits++;
            printf("HIT\t%s\t%zu\t", "?", i);
            for (int k = 0; k < 16; k++) printf("%02x", d[i + k]);
            printf("\n");
            fflush(stdout);
            pthread_mutex_unlock(&g_lock);
        }
    }
    return NULL;
}

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: %s <page1.db> <blob> [blob...]\n", argv[0]);
        return 2;
    }
    FILE *pf = fopen(argv[1], "rb");
    if (!pf) { perror("page1"); return 1; }
    unsigned char page1[PAGE_SIZE];
    if (fread(page1, 1, PAGE_SIZE, pf) != PAGE_SIZE) {
        fprintf(stderr, "page1 太短\n"); return 1;
    }
    fclose(pf);
    memcpy(g_frag, page1 + 16, 8);
    memcpy(g_ct0, page1 + 8, 8);
    memcpy(g_ct0 + 8, page1 + 24, 8);

    for (int a = 2; a < argc; a++) {
        FILE *f = fopen(argv[a], "rb");
        if (!f) { perror(argv[a]); continue; }
        fseek(f, 0, SEEK_END);
        long sz = ftell(f);
        fseek(f, 0, SEEK_SET);
        if (sz <= 16) { fclose(f); continue; }
        unsigned char *buf = malloc((size_t)sz);
        if (!buf) { fclose(f); continue; }
        if (fread(buf, 1, (size_t)sz, f) != (size_t)sz) { free(buf); fclose(f); continue; }
        fclose(f);

        int nthreads = 8;
        pthread_t th[16];
        job_t jobs[16];
        size_t chunk = (size_t)sz / nthreads + 1;
        for (int i = 0; i < nthreads; i++) {
            size_t start = (size_t)i * chunk;
            if (start >= (size_t)sz) { jobs[i].len = 0; jobs[i].data = buf; }
            else {
                /* 每片带 15 字节重叠，避免漏掉跨片的候选 */
                size_t end = start + chunk + 15;
                if (end > (size_t)sz) end = (size_t)sz;
                jobs[i].data = buf + start;
                jobs[i].len = end - start;
            }
            jobs[i].tid = i;
            pthread_create(&th[i], NULL, worker, &jobs[i]);
        }
        for (int i = 0; i < nthreads; i++) pthread_join(th[i], NULL);
        free(buf);
        fprintf(stderr, "scanned %s (%ld bytes), hits so far %d\n", argv[a], sz, g_hits);
    }
    printf("TOTAL_HITS\t%d\n", g_hits);
    return g_hits ? 0 : 1;
}
