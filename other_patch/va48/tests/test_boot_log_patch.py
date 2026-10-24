"""屏幕日志补丁的预检、布局校验和生成 C 边界测试；不代替内核构建或实机测试。"""
import contextlib
import importlib.util
import io
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'patch_boot_log.py'
SPEC = importlib.util.spec_from_file_location('boot_log_patch', SCRIPT)
PATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCH)
LAYOUT = 'base=0x10000000,width=80,height=96,stride=320,format=x8r8g8b8'


class PatchTests(unittest.TestCase):
    def fixture(self, root, version='6.1.118', annotated=True):
        major, minor, sub = version.split('.')
        (root / 'Makefile').write_text(f'VERSION = {major}\nPATCHLEVEL = {minor}\nSUBLEVEL = {sub}\n')
        for rel, edits in PATCH.EDITS.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            text = '\n'.join(old for _, old, _ in edits)
            text = text.replace('@SETUP_ARCH@', 'void __init ' +
                                ('__no_sanitize_address ' if annotated else '') +
                                'setup_arch(char **cmdline_p)\n')
            path.write_text(text)

    def run_patch(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as capture:
            status = PATCH.main([str(arg) for arg in args])
        return status, capture.getvalue()

    def test_all_profiles_and_signature_variants(self):
        for annotated in (False, True):
            for profile in ('auto', 'oneplus12-legacy', 'custom'):
                with self.subTest(annotated=annotated, profile=profile), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    self.fixture(root, annotated=annotated)
                    args = [root, '--profile', profile]
                    if profile == 'custom':
                        args += ['--framebuffer', LAYOUT]
                    original = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
                    status, out = self.run_patch(*args, '--check')
                    self.assertEqual(status, 0, out)
                    self.assertIn('checked=8 expected=8 failed=0', out)
                    self.assertEqual(original, {p: p.read_bytes() for p in original})
                    status, out = self.run_patch(*args)
                    self.assertEqual(status, 0, out)
                    self.assertIn('applied=8 expected=8 failed=0', out)
                    self.assertIn('\tdefault y\n', (root / 'lib/fonts/Kconfig').read_text())
                    patched = {p: p.read_bytes() for p in original}
                    status, out = self.run_patch(*args)
                    self.assertEqual(status, 1, out)
                    self.assertEqual(patched, {p: p.read_bytes() for p in patched})

    def test_rejects_other_kernel_and_missing_anchor_without_writes(self):
        for version, missing in (('6.6.0', False), ('6.1.57', True)):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.fixture(root, version)
                if missing:
                    path = root / 'init/main.c'
                    path.write_text(path.read_text().replace('ret = fn();', 'ret = custom_fn();'))
                original = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
                status, out = self.run_patch(root)
                self.assertEqual(status, 1, out)
                self.assertIn('no files written', out)
                self.assertEqual(original, {p: p.read_bytes() for p in original})

    def test_missing_font_dependency_anchor_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            path = root / 'lib/fonts/Kconfig'
            path.write_text(path.read_text().replace('config FONT_SUPPORT', 'config CUSTOM_FONT_SUPPORT'))
            original = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
            status, out = self.run_patch(root)
            self.assertEqual(status, 1, out)
            self.assertIn('no files written', out)
            self.assertEqual(original, {p: p.read_bytes() for p in original})

    def test_layout_rejects_bad_dimensions_overflow_and_code(self):
        bad = [LAYOUT + ',width=80', LAYOUT.replace('width=80', 'width=15'),
               LAYOUT.replace('height=96', 'height=95'), LAYOUT.replace('stride=320', 'stride=316'),
               LAYOUT.replace('stride=320', 'stride=321'), LAYOUT.replace('base=0x10000000', 'base=3'),
               LAYOUT.replace('base=0x10000000', 'base=0xfffffffffffffff0'),
               LAYOUT.replace('format=x8r8g8b8', 'format=r5g6b5'),
               LAYOUT.replace('base=0x10000000', 'base=0;panic()'),
               LAYOUT.replace('height=96', 'height=8193'),
               'base=4096,width=8192,height=8192,stride=32768,format=x8r8g8b8']
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                PATCH.framebuffer_config(value)
        self.assertEqual(PATCH.framebuffer_config(LAYOUT)['stride'], 320)
        self.assertNotIn('0xd5100000', PATCH.render_helpers('auto'))
        self.assertIn('0xd5100000', PATCH.render_helpers('oneplus12-legacy'))

    @unittest.skipUnless(shutil.which('gcc'), 'host C 测试需要 GCC')
    def test_generated_c_with_sanitizers(self):
        helpers = re.sub(r'^#include.*\n', '', PATCH.render_helpers('auto'), flags=re.M)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'test.c'
            binary = root / 'test'
            source.write_text(C_STUB + helpers + C_TEST)
            subprocess.run(['gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                            '-Wno-unused-parameter', '-Wno-unused-variable',
                            '-Wno-unused-function', '-fsanitize=address,undefined',
                            '-fno-omit-frame-pointer', str(source), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)


C_STUB = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define __init
#define core_initcall(fn)
#define min_t(type,a,b) ((type)(a)<(type)(b)?(type)(a):(type)(b))
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,v) ((x)=(v))
#define smp_load_acquire(p) (*(p))
#define smp_store_release(p,v) (*(p)=(v))
#define ATOMIC_INIT(x) (x)
#define MEMREMAP_WB 1
typedef unsigned char u8;
typedef uint32_t u32;
typedef uint64_t u64;
typedef uint64_t phys_addr_t;
typedef int (*initcall_t)(void);
typedef int atomic_t;
static int atomic_cmpxchg(int *p,int old,int value) { int r=*p; if(r==old)*p=value; return r; }
static void atomic_set(int *p,int value) {*p=value;}
#define font_desc host_font
struct host_font { const unsigned char *data; };
static struct host_font font_vga_8x16;
static void dcache_clean_poc(unsigned long a,unsigned long b) {(void)a;(void)b;}
static int warnings, emergencies, panic_timeout=7;
#define pr_warn(...) (++warnings)
#define pr_emerg(...) (++emergencies)
#define pr_err(...) (++warnings)
struct kmsg_dump_iter {int stub;};
enum kmsg_dump_reason {KMSG_DUMP_UNDEF,KMSG_DUMP_PANIC,KMSG_DUMP_OOPS};
struct kmsg_dumper {void (*dump)(struct kmsg_dumper *,enum kmsg_dump_reason);enum kmsg_dump_reason max_reason;};
static int kmsg_dump_register(struct kmsg_dumper *d) {return 0;}
static void kmsg_dump_rewind(struct kmsg_dump_iter *i) {}
static void kmsg_dump_get_buffer(struct kmsg_dump_iter *i,bool b,char *p,size_t n,size_t *len) {
  const char *text="PC: fake\nCall trace:\n  fake_caller\n";
  *len=strlen(text); assert(*len<n);memcpy(p,text,*len);
}
static unsigned char pixels[320*96];
static bool mapping_fails;
static void *memremap(phys_addr_t p,size_t n,int mode) {
  assert(p==0x10000000);assert(n==sizeof(pixels));return mapping_fails?NULL:pixels;
}
static phys_addr_t reserved_start=0x10000000,reserved_end=0x10000000+sizeof(pixels);
#define for_each_reserved_mem_range(i,s,e) \
  for((i)=0; (i)<1 && ((*(s)=reserved_start),(*(e)=reserved_end),1); ++(i))
struct device_node {int index;};
struct resource {phys_addr_t start,end;};
static int child_count, bad_field;
static bool chosen_exists=true,available=true,compatible=true;
static const char *pixel_format="x8r8g8b8";
static struct device_node chosen,children[2]={{0},{1}};
static struct device_node *of_find_node_by_path(const char *p) {return chosen_exists?&chosen:NULL;}
static void of_node_put(struct device_node *p) {}
#define for_each_available_child_of_node(c,n) \
  for((n)=children;available && (n)<children+child_count;++(n))
static bool of_device_is_compatible(struct device_node *n,const char *v) {return compatible;}
static int of_address_to_resource(struct device_node *n,int i,struct resource *r) {
  r->start=0x10000000;r->end=r->start+sizeof(pixels)-1;return bad_field==1;
}
#define resource_size(r) ((r)->end-(r)->start+1)
static int of_property_read_u32(struct device_node *n,const char *key,u32 *v) {
  if(!strcmp(key,"width")){*v=80;return bad_field==2;}
  if(!strcmp(key,"height")){*v=96;return bad_field==3;}
  if(!strcmp(key,"stride")){*v=320;return bad_field==4;}
  return 1;
}
static int of_property_read_string(struct device_node *n,const char *key,const char **v) {
  *v=pixel_format;return bad_field==5;
}
static struct { const char *comm; } current_task={"test"};
#define current (&current_task)
static int task_pid_nr(void *p) {return 1;}
'''

C_TEST = r'''
static int fake_initcall(void){return 0;}
int main(void) {
  static unsigned char glyphs[256*16];
  char text[600];
  struct bootlog_fb_config valid={.base=0x10000000,.width=80,.height=96,.stride=320,.span=sizeof(pixels)};
  assert(bootlog_valid_layout(&valid));
  valid.stride=319;assert(!bootlog_valid_layout(&valid));valid.stride=320;
  valid.span=sizeof(pixels)-1;assert(!bootlog_valid_layout(&valid));valid.span=sizeof(pixels);
  valid.base=UINT64_MAX-3;assert(!bootlog_valid_layout(&valid));valid.base=0x10000000;
  assert(bootlog_reserved(&valid));reserved_start++;assert(!bootlog_reserved(&valid));reserved_start--;
  bootlog_init(); assert(!bootlog_fb_validated); assert(warnings==1);
  bootlog_dump(NULL,KMSG_DUMP_PANIC);assert(panic_timeout==7);
  child_count=1;assert(bootlog_read_simplefb());
  child_count=2;assert(!bootlog_read_simplefb());child_count=1;
  for(bad_field=1;bad_field<=5;bad_field++) {
    assert(!bootlog_read_simplefb());
  }
  bad_field=0;
  pixel_format="r5g6b5";assert(!bootlog_read_simplefb());pixel_format="a8b8g8r8";
  chosen_exists=false;assert(!bootlog_read_simplefb());chosen_exists=true;
  bootlog_init();assert(bootlog_fb_validated);
  mapping_fails=true;bootlog_map_screen();assert(!bootlog_ready);mapping_fails=false;
  memset(glyphs,0xff,sizeof(glyphs));font_vga_8x16.data=glyphs;
  bootlog_map_screen();assert(bootlog_ready);assert(bootlog_cols==5);assert(bootlog_rows==3);
  memset(text,'x',sizeof(text));text[sizeof(text)-1]=0;
  bootlog_line(text,bootlog_rows-1);bootlog_line(text,bootlog_rows);
  bootlog_line(NULL,0);
  assert(bootlog_next_row("123456\n",7,0)==5);
  assert(bootlog_next_row("12345\n",6,0)==6);
  assert(bootlog_tail_start("aaa\nbbb\nccc\n",12,2)==4);
  bootlog_initcall_start(fake_initcall);bootlog_initcall_finish(fake_initcall,-1);
  bootlog_userspace();bootlog_reboot("test");
  bootlog_dump(NULL,KMSG_DUMP_PANIC);assert(panic_timeout==0);
  return 0;
}
'''


if __name__ == '__main__':
    unittest.main()
