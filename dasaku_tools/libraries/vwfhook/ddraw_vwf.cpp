// ddraw_vwf.cpp - VWF (variable-width font) hook
//   1. Forwards DirectDrawCreateEx to the real system ddraw.dll so the game runs.
//   2. IAT-hooks GDI32!CreateFontA (force Meiryo+SHIFT-JIS) and GDI32!TextOutA
//      (draw full-width-ASCII glyphs as their half-width form).
//   3. Patches three sites in the engine's line renderer FUN_004b3be0 so per-glyph advance
//      and line output width become proportional, keyed by a width table (g_frac) measured
//      from Meiryo. Japanese (frac=256) stays byte-identical to the original.
// All engine addresses are RVAs from imagebase 0x400000, rebased onto the live module.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <cstdint>

static const uint32_t RVA_P1 = 0xb4001;    // shadow pass:  ADD EBX,[EBP-0x3c]; ADD EDX,2   (EDX=glyph ptr)
static const uint32_t RVA_P1R = 0xb4007;   //   return: DEC [EBP-0xc]
static const uint32_t RVA_P2 = 0xb4382;    // main pass:    ADD EDI,[EBP-0x3c]; MOV EAX,[EBP-0x38] (ESI=glyph ptr)
static const uint32_t RVA_P2R = 0xb4388;   //   return: INC EBX
static const uint32_t RVA_P3 = 0xb4065;    // line width:   IMUL EAX,[EBP-0x14]; MOV EDI,[EBP-0x8]
static const uint32_t RVA_P3R = 0xb406c;   //   return: MOV EDX,[EBX+0xc]
static const uint32_t RVA_MODE = 0x1645d8; // DAT_005645d8 (==2 normal full-width path)
static const uint32_t RVA_GBUF = 0x1849b0; // DAT_005849b0 glyph buffer (2 bytes/glyph in mode 2)
static const uint32_t RVA_IAT_CREATEFONTA = 0x11f024;
static const uint32_t RVA_IAT_TEXTOUTA = 0x11f02c;
static const uint32_t RVA_FWTABLE = 0x163d38; // DAT_00563d38 ASCII->full-width SJIS table (idx = char*2)

#if defined(__has_include)
#if __has_include("vwf_font.h")
#include "vwf_font.h"
#endif
#endif
#ifndef TL_FACE_A
#define TL_FACE_A "Noto Sans JP"
#define TL_FACE_W L"Noto Sans JP"
#define TL_WEIGHT FW_NORMAL // engine default 700
#define EN_HEIGHT_PCT 100   // English glyph height as % of the JP cell em
#endif
#ifndef TL_FONT_FILE_W
#define TL_FONT_FILE_W L""
#endif

static uintptr_t g_base = 0;
static WORD g_frac[0x10000];
static char g_rev[0x10000];
static wchar_t g_ext[0x10000];
static BYTE *g_stub = nullptr;
static volatile LONG g_fracReady = 0;

static int g_emRefPx = 1000;
static int g_tmHeightRef = 1000;
static HFONT g_redFont = nullptr;
static int g_redEm = -1;

#define VWF_DEBUG 0 // set to 1 to write game\vwf_log.txt
static void Log(const char *s)
{
#if !VWF_DEBUG
    (void)s;
    return;
#else
    HANDLE h = CreateFileA("vwf_log.txt", FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                           NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE)
        return;
    SetFilePointer(h, 0, NULL, FILE_END);
    DWORD w;
    WriteFile(h, s, lstrlenA(s), &w, NULL);
    CloseHandle(h);
#endif
}
static void LogF(const char *fmt, ...)
{
    char buf[1024];
    va_list ap;
    va_start(ap, fmt);
    wvsprintfA(buf, fmt, ap);
    va_end(ap);
    Log(buf);
}

typedef BOOL(WINAPI *TextOutA_t)(HDC, int, int, LPCSTR, int);
typedef BOOL(WINAPI *TextOutW_t)(HDC, int, int, LPCWSTR, int);
typedef HFONT(WINAPI *CreateFontA_t)(int, int, int, int, int, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, LPCSTR);
static TextOutA_t g_realTextOutA = nullptr;
static TextOutW_t g_realTextOutW = nullptr;
static CreateFontA_t g_realCreateFontA = nullptr;

static void BuildRevMap()
{
    const BYTE *fw = (const BYTE *)(g_base + RVA_FWTABLE);
    for (int i = 0; i < 0x10000; ++i)
        g_frac[i] = 256; // default: full width JP
    for (int c = 0x20; c <= 0x7e; ++c)
    {
        BYTE b0 = fw[c * 2 + 0];
        BYTE b1 = fw[c * 2 + 1];
        if (b0 == 0 && b1 == 0)
            continue;
        WORD le = (WORD)(b0 | (b1 << 8));
        g_rev[le] = (char)c;
    }
}

static void RefineFracFromMeiryo()
{
    if (InterlockedExchange(&g_fracReady, 1))
        return; // once
    HDC dc = CreateCompatibleDC(NULL);
    if (!dc)
        return;
    const int R = 1000; // reference cell height
    HFONT f = CreateFontW(R, 0, 0, 0, TL_WEIGHT, 0, 0, 0,
                          SHIFTJIS_CHARSET, OUT_TT_PRECIS, CLIP_DEFAULT_PRECIS,
                          ANTIALIASED_QUALITY, DEFAULT_PITCH | FF_DONTCARE, TL_FACE_W);
    HGDIOBJ old = SelectObject(dc, f);
    char face[64] = {0};
    GetTextFaceA(dc, 64, face);
    TEXTMETRICA tm = {0};
    GetTextMetricsA(dc, &tm);
    wchar_t emCh = 0x3042;
    SIZE emSz = {R, R};
    GetTextExtentPoint32W(dc, &emCh, 1, &emSz);
    LogF("RefineFrac: face=%s tmHeight=%d cjkAdv=%d enHeight%%=%d\r\n",
         face, tm.tmHeight, emSz.cx, EN_HEIGHT_PCT);

    // Render EN at proportional height
    HFONT fr = CreateFontW(R * EN_HEIGHT_PCT / 100, 0, 0, 0, TL_WEIGHT, 0, 0, 0,
                           SHIFTJIS_CHARSET, OUT_TT_PRECIS, CLIP_DEFAULT_PRECIS,
                           ANTIALIASED_QUALITY, DEFAULT_PITCH | FF_DONTCARE, TL_FACE_W);
    SelectObject(dc, fr);
    for (int c = 0x20; c <= 0x7e; ++c)
    {
        const BYTE *fw = (const BYTE *)(g_base + RVA_FWTABLE);
        BYTE b0 = fw[c * 2], b1 = fw[c * 2 + 1];
        if (b0 == 0 && b1 == 0)
            continue;
        WORD le = (WORD)(b0 | (b1 << 8));
        wchar_t wc = (wchar_t)c;
        SIZE s = {0, 0};
        GetTextExtentPoint32W(dc, &wc, 1, &s);
        int frac = (int)((s.cx * 256 + R / 2) / R);
        if (frac < 1)
            frac = 1;
        if (frac > 256)
            frac = 256;
        g_frac[le] = (WORD)frac;
    }

    {
        BYTE trails[58];
        int nt = 0;
        for (int t = 0x01; t < 0x40; ++t)
            if (t != 0x09 && t != 0x0A && t != 0x0D && t != 0x20 && t != 0x2C)
                trails[nt++] = (BYTE)t;
        HANDLE hf = CreateFileA("sjis_ext.bin", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                NULL, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
        if (hf != INVALID_HANDLE_VALUE)
        {
            DWORD sz = GetFileSize(hf, NULL), rd = 0;
            if (sz > 0 && sz < 0x40000)
            {
                BYTE *buf = (BYTE *)HeapAlloc(GetProcessHeap(), 0, sz);
                if (buf && ReadFile(hf, buf, sz, &rd, NULL))
                {
                    int n = rd / 2;
                    for (int idx = 0; idx < n && idx < 58 * 0x1f; ++idx)
                    {
                        wchar_t u = (wchar_t)(buf[idx * 2] | (buf[idx * 2 + 1] << 8));
                        int lead = 0x81 + idx / nt, trail = trails[idx % nt];
                        WORD le = (WORD)(lead | (trail << 8));
                        g_ext[le] = u;
                        SIZE s = {0, 0};
                        GetTextExtentPoint32W(dc, &u, 1, &s);
                        int frac = (int)((s.cx * 256 + R / 2) / R);
                        if (frac < 1)
                            frac = 1;
                        if (frac > 256)
                            frac = 256;
                        g_frac[le] = (WORD)frac;
                    }
                    LogF("loaded sjis_ext.bin: %d tunnel chars\r\n", n);
                }
                if (buf)
                    HeapFree(GetProcessHeap(), 0, buf);
            }
            CloseHandle(hf);
        }
    }

    {
        static const struct
        {
            WORD le;
            wchar_t u;
        } PUNCT[] = {
            // SJIS -> Unicode
            {0x6381, 0x2026},
            {0x6781, 0x201C},
            {0x6881, 0x201D},
            {0x6581, 0x2018},
            {0x6681, 0x2019},
        };
        for (int k = 0; k < (int)(sizeof(PUNCT) / sizeof(PUNCT[0])); ++k)
        {
            g_ext[PUNCT[k].le] = PUNCT[k].u;
            wchar_t u = PUNCT[k].u;
            SIZE s = {0, 0};
            GetTextExtentPoint32W(dc, &u, 1, &s);
            int frac = (int)((s.cx * 256 + R / 2) / R);
            if (frac < 1)
                frac = 1;
            if (frac > 256)
                frac = 256;
            g_frac[PUNCT[k].le] = (WORD)frac;
        }
    }

    SelectObject(dc, old);
    DeleteObject(fr);
    DeleteObject(f);
    DeleteDC(dc);

    for (int c = 0x20; c <= 0x7e; ++c)
    {
        const BYTE *fw = (const BYTE *)(g_base + RVA_FWTABLE);
        BYTE b0 = fw[c * 2], b1 = fw[c * 2 + 1];
        WORD le = (WORD)(b0 | (b1 << 8));
        LogF("frac['%c'=%02x] sjis=%04x -> %d\r\n", (c >= 0x20 && c < 0x7f) ? c : '?', c, le, g_frac[le]);
    }
}

static HFONT WINAPI Hook_CreateFontA(int h, int w, int esc, int ori, int wt, DWORD it, DWORD un, DWORD so,
                                     DWORD cs, DWORD op, DWORD cp, DWORD q, DWORD pf, LPCSTR face)
{
    RefineFracFromMeiryo();
    LogF("CreateFontA: h=%d w=%d weight=%d -> face=%s\r\n", h, w, wt, TL_FACE_A);
    return g_realCreateFontA(h, 0, esc, ori, TL_WEIGHT, it, un, so, SHIFTJIS_CHARSET, op, cp,
                             q, pf, TL_FACE_A);
}

static BOOL WINAPI Hook_TextOutA(HDC dc, int x, int y, LPCSTR s, int c)
{
    if (c == 2 && s)
    {
        BYTE b0 = (BYTE)s[0], b1 = (BYTE)s[1];
        WORD le = (WORD)(b0 | (b1 << 8));
        char a = g_rev[le];
        wchar_t u = g_ext[le];
        if (a || (u && g_realTextOutW))
        {
            TEXTMETRICA tf = {0};
            if (GetTextMetricsA(dc, &tf) && tf.tmHeight > 0)
            {
                int hEn = tf.tmHeight * EN_HEIGHT_PCT / 100;
                if (hEn > 0)
                {
                    if (g_redEm != hEn)
                    {
                        if (g_redFont)
                            DeleteObject(g_redFont);
                        g_redFont = g_realCreateFontA(hEn, 0, 0, 0, TL_WEIGHT, 0, 0, 0,
                                                      SHIFTJIS_CHARSET, OUT_TT_PRECIS, CLIP_DEFAULT_PRECIS,
                                                      ANTIALIASED_QUALITY, DEFAULT_PITCH | FF_DONTCARE, TL_FACE_A);
                        g_redEm = hEn;
                    }
                    if (g_redFont)
                    {
                        HGDIOBJ oldf = SelectObject(dc, g_redFont);
                        TEXTMETRICA tr = {0};
                        GetTextMetricsA(dc, &tr);
                        int yEn = y + (tf.tmAscent - tr.tmAscent);
                        BOOL r = u ? g_realTextOutW(dc, x, yEn, &u, 1)
                                   : g_realTextOutA(dc, x, yEn, &a, 1);
                        SelectObject(dc, oldf);
                        return r;
                    }
                }
            }
            return u ? g_realTextOutW(dc, x, y, &u, 1) : g_realTextOutA(dc, x, y, &a, 1);
        }
    }
    return g_realTextOutA(dc, x, y, s, c);
}

static void InstallIatHook(uint32_t rvaSlot, void *hook, void **saveOrig)
{
    void **slot = (void **)(g_base + rvaSlot);
    DWORD old;
    VirtualProtect(slot, sizeof(void *), PAGE_READWRITE, &old);
    *saveOrig = *slot;
    *slot = hook;
    VirtualProtect(slot, sizeof(void *), old, &old);
}

struct Emit
{
    BYTE *p;
    void b(BYTE v) { *p++ = v; }
    void d(uint32_t v)
    {
        *(uint32_t *)p = v;
        p += 4;
    }
    BYTE *here() { return p; }
};

static void PatchSite(uint32_t rvaSite, BYTE *stub, int siteLen)
{
    BYTE *site = (BYTE *)(g_base + rvaSite);
    DWORD old;
    VirtualProtect(site, siteLen, PAGE_EXECUTE_READWRITE, &old);
    site[0] = 0xE9;
    *(uint32_t *)(site + 1) = (uint32_t)(stub - (site + 5));
    for (int i = 5; i < siteLen; ++i)
        site[i] = 0x90;
    VirtualProtect(site, siteLen, old, &old);
    FlushInstructionCache(GetCurrentProcess(), site, siteLen);
}

static void BuildStubsAndPatch()
{
    g_stub = (BYTE *)VirtualAlloc(NULL, 0x400, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!g_stub)
        return;
    uint32_t MODE = (uint32_t)(g_base + RVA_MODE);
    uint32_t FRAC = (uint32_t)(uintptr_t)&g_frac[0];
    uint32_t GBUF = (uint32_t)(g_base + RVA_GBUF);

    Emit e;
    e.p = g_stub;

    // ---------- Stub P1 (EDX=glyph ptr, EBX=x) -> add EBX prop; add EDX,2; jmp P1R ----------
    BYTE *s1 = e.here();
    e.b(0x83);
    e.b(0x3D);
    e.d(MODE);
    e.b(0x02); // cmp dword[MODE],2
    BYTE *j1 = e.here();
    e.b(0x75);
    e.b(0x00); // jne .orig
    e.b(0x0F);
    e.b(0xB7);
    e.b(0x02); // movzx eax,word[edx]
    e.b(0x0F);
    e.b(0xB7);
    e.b(0x04);
    e.b(0x45);
    e.d(FRAC); // movzx eax,word[FRAC+eax*2]
    e.b(0x0F);
    e.b(0xAF);
    e.b(0x45);
    e.b(0xC4); // imul eax,[ebp-0x3c]
    e.b(0xC1);
    e.b(0xF8);
    e.b(0x08); // sar eax,8
    e.b(0x01);
    e.b(0xC3); // add ebx,eax
    BYTE *k1 = e.here();
    e.b(0xEB);
    e.b(0x00); // jmp .after
    BYTE *o1 = e.here();
    *(j1 + 1) = (BYTE)(o1 - (j1 + 2)); // .orig:
    e.b(0x03);
    e.b(0x5D);
    e.b(0xC4); // add ebx,[ebp-0x3c]
    BYTE *a1 = e.here();
    *(k1 + 1) = (BYTE)(a1 - (k1 + 2)); // .after:
    e.b(0x83);
    e.b(0xC2);
    e.b(0x02); // add edx,2
    e.b(0xE9);
    e.d((uint32_t)((g_base + RVA_P1R) - (uintptr_t)(e.here() + 4))); // jmp P1R

    // ---------- Stub P2 (ESI=glyph ptr, EDI=x) -> add EDI prop; mov eax,[ebp-0x38]; jmp P2R ----------
    BYTE *s2 = e.here();
    e.b(0x83);
    e.b(0x3D);
    e.d(MODE);
    e.b(0x02);
    BYTE *j2 = e.here();
    e.b(0x75);
    e.b(0x00);
    e.b(0x0F);
    e.b(0xB7);
    e.b(0x06); // movzx eax,word[esi]
    e.b(0x0F);
    e.b(0xB7);
    e.b(0x04);
    e.b(0x45);
    e.d(FRAC);
    e.b(0x0F);
    e.b(0xAF);
    e.b(0x45);
    e.b(0xC4);
    e.b(0xC1);
    e.b(0xF8);
    e.b(0x08);
    e.b(0x01);
    e.b(0xC7); // add edi,eax
    BYTE *k2 = e.here();
    e.b(0xEB);
    e.b(0x00);
    BYTE *o2 = e.here();
    *(j2 + 1) = (BYTE)(o2 - (j2 + 2));
    e.b(0x03);
    e.b(0x7D);
    e.b(0xC4); // add edi,[ebp-0x3c]
    BYTE *a2 = e.here();
    *(k2 + 1) = (BYTE)(a2 - (k2 + 2));
    e.b(0x8B);
    e.b(0x45);
    e.b(0xC8); // mov eax,[ebp-0x38]
    e.b(0xE9);
    e.d((uint32_t)((g_base + RVA_P2R) - (uintptr_t)(e.here() + 4)));

    // ---------- Stub P3 (EAX=base1x) -> EAX=sum(prop)/2; mov edi,[ebp-0x8]; jmp P3R ----------
    BYTE *s3 = e.here();
    e.b(0x83);
    e.b(0x3D);
    e.d(MODE);
    e.b(0x02);
    BYTE *j3 = e.here();
    e.b(0x75);
    e.b(0x00); // jne .orig
    e.b(0x51);
    e.b(0x56);
    e.b(0x52); // push ecx; push esi; push edx
    e.b(0x8B);
    e.b(0x4D);
    e.b(0xEC); // mov ecx,[ebp-0x14] (numChars)
    e.b(0x85);
    e.b(0xC9); // test ecx,ecx
    BYTE *jg = e.here();
    e.b(0x7F);
    e.b(0x00); // jg .calc
    e.b(0x31);
    e.b(0xC0); // xor eax,eax
    BYTE *kz = e.here();
    e.b(0xEB);
    e.b(0x00); // jmp .restore
    BYTE *calc = e.here();
    *(jg + 1) = (BYTE)(calc - (jg + 2));
    e.b(0xBE);
    e.d(GBUF); // mov esi,GBUF
    e.b(0x31);
    e.b(0xD2); // xor edx,edx
    BYTE *loop = e.here();
    e.b(0x0F);
    e.b(0xB7);
    e.b(0x06); // movzx eax,word[esi]
    e.b(0x0F);
    e.b(0xB7);
    e.b(0x04);
    e.b(0x45);
    e.d(FRAC);
    e.b(0x0F);
    e.b(0xAF);
    e.b(0x45);
    e.b(0xC4); // imul eax,[ebp-0x3c]
    e.b(0xC1);
    e.b(0xF8);
    e.b(0x08); // sar eax,8
    e.b(0x01);
    e.b(0xC2); // add edx,eax
    e.b(0x83);
    e.b(0xC6);
    e.b(0x02); // add esi,2
    e.b(0x49); // dec ecx
    BYTE *jnz = e.here();
    e.b(0x75);
    e.b(0x00); // jnz .loop
    *(jnz + 1) = (BYTE)(loop - (jnz + 2));
    e.b(0x89);
    e.b(0xD0); // mov eax,edx
    e.b(0xD1);
    e.b(0xF8); // sar eax,1   (2x -> 1x)

    e.b(0x8B);
    e.b(0x4D);
    e.b(0xC4); // mov ecx,[ebp-0x3c] (pitch; ecx free, loop ended at 0)
    e.b(0xD1);
    e.b(0xF9); // sar ecx,1          (ecx = base1x = cell)
    e.b(0x8D);
    e.b(0x44);
    e.b(0x08);
    e.b(0xFF); // lea eax,[eax+ecx-1]
    e.b(0x99); // cdq
    e.b(0xF7);
    e.b(0xF9); // idiv ecx
    e.b(0x0F);
    e.b(0xAF);
    e.b(0xC1); // imul eax,ecx       (eax = ceil(w/cell)*cell)
    BYTE *rest = e.here();
    *(kz + 1) = (BYTE)(rest - (kz + 2));
    e.b(0x5A);
    e.b(0x5E);
    e.b(0x59); // pop edx; pop esi; pop ecx
    BYTE *kf = e.here();
    e.b(0xEB);
    e.b(0x00); // jmp .after
    BYTE *o3 = e.here();
    *(j3 + 1) = (BYTE)(o3 - (j3 + 2));
    e.b(0x0F);
    e.b(0xAF);
    e.b(0x45);
    e.b(0xEC); // imul eax,[ebp-0x14]
    BYTE *a3 = e.here();
    *(kf + 1) = (BYTE)(a3 - (kf + 2));
    e.b(0x8B);
    e.b(0x7D);
    e.b(0xF8); // mov edi,[ebp-0x8]
    e.b(0xE9);
    e.d((uint32_t)((g_base + RVA_P3R) - (uintptr_t)(e.here() + 4)));

    FlushInstructionCache(GetCurrentProcess(), g_stub, 0x400);

    BYTE *p1 = (BYTE *)(g_base + RVA_P1);
    BYTE *p2 = (BYTE *)(g_base + RVA_P2);
    BYTE *p3 = (BYTE *)(g_base + RVA_P3);
    const BYTE e1[6] = {0x03, 0x5D, 0xC4, 0x83, 0xC2, 0x02};
    const BYTE e2[6] = {0x03, 0x7D, 0xC4, 0x8B, 0x45, 0xC8};
    const BYTE e3[7] = {0x0F, 0xAF, 0x45, 0xEC, 0x8B, 0x7D, 0xF8};
    bool ok = true;
    for (int i = 0; i < 6; i++)
        if (p1[i] != e1[i])
            ok = false;
    for (int i = 0; i < 6; i++)
        if (p2[i] != e2[i])
            ok = false;
    for (int i = 0; i < 7; i++)
        if (p3[i] != e3[i])
            ok = false;
    LogF("BuildStubsAndPatch: base=%08x stub=%08x sitesOK=%d\r\n", (uint32_t)g_base, (uint32_t)(uintptr_t)g_stub, ok ? 1 : 0);
    LogF("  P1 bytes %02x %02x %02x %02x %02x %02x\r\n", p1[0], p1[1], p1[2], p1[3], p1[4], p1[5]);
    LogF("  P2 bytes %02x %02x %02x %02x %02x %02x\r\n", p2[0], p2[1], p2[2], p2[3], p2[4], p2[5]);
    LogF("  P3 bytes %02x %02x %02x %02x %02x %02x %02x\r\n", p3[0], p3[1], p3[2], p3[3], p3[4], p3[5], p3[6]);
    if (!ok)
    {
        Log("[vwf] site byte mismatch; aborting code patch\r\n");
        return;
    }

    PatchSite(RVA_P1, s1, 6);
    PatchSite(RVA_P2, s2, 6);
    PatchSite(RVA_P3, s3, 7);
    Log("[vwf] code patches applied\r\n");
}

static void Init()
{
    g_base = (uintptr_t)GetModuleHandleW(NULL);
    if (!g_base)
        return;
    Log("\r\n=== vwf Init ===\r\n");
    // Load custom font here
    if (TL_FONT_FILE_W[0])
    {
        int n = AddFontResourceExW(TL_FONT_FILE_W, FR_PRIVATE, NULL);
        LogF("AddFontResourceEx(%S) -> %d face(s)\r\n", TL_FONT_FILE_W, n);
    }
    g_realTextOutW = (TextOutW_t)GetProcAddress(GetModuleHandleW(L"gdi32.dll"), "TextOutW");
    BuildRevMap();
    InstallIatHook(RVA_IAT_CREATEFONTA, (void *)&Hook_CreateFontA, (void **)&g_realCreateFontA);
    InstallIatHook(RVA_IAT_TEXTOUTA, (void *)&Hook_TextOutA, (void **)&g_realTextOutA);
    BuildStubsAndPatch();
}

extern "C" HRESULT WINAPI
DirectDrawCreateEx(void *g, void **p, void *iid, void *u)
{
    typedef HRESULT(WINAPI * fn_t)(void *, void **, void *, void *);
    static fn_t real = nullptr;
    if (!real)
    {
        char sys[MAX_PATH];
        GetSystemDirectoryA(sys, MAX_PATH);
        lstrcatA(sys, "\\ddraw.dll");
        HMODULE h = LoadLibraryA(sys);
        if (h)
            real = (fn_t)GetProcAddress(h, "DirectDrawCreateEx");
    }
    if (!real)
        return E_FAIL;
    return real(g, p, iid, u);
}

BOOL WINAPI DllMain(HINSTANCE hinst, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH)
    {
        DisableThreadLibraryCalls(hinst);
        Init();
    }
    return TRUE;
}
