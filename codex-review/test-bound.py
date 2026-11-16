import os, pathlib, tempfile, unittest
workflow=pathlib.Path(__file__).parents[1]/'.github/workflows/code-review.yml'
code=workflow.read_text().split('# BEGIN BOUND\n')[1].split('# END BOUND')[0]
import textwrap
code=textwrap.dedent(code)
class BoundTests(unittest.TestCase):
    def test_byte_cap_before_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp)
            (root/'codex-review.txt').write_text('한글@`'*100000)
            env={'RUNNER_TEMP':tmp,'GITHUB_OUTPUT':tmp+'/output','GITHUB_STEP_SUMMARY':tmp+'/summary'}
            from unittest.mock import patch
            with patch.dict(os.environ,env): exec(compile(code,'bound','exec'),{})
            output=(root/'output').read_text()
            self.assertLess(len(output.encode()),49000)
            self.assertIn('truncated',output)
            self.assertNotIn('\ufffd',output)
            self.assertIn('@\u200b',(root/'summary').read_text())
    def test_empty_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp,'codex-review.txt').touch()
            from unittest.mock import patch
            with patch.dict(os.environ,{'RUNNER_TEMP':tmp}), self.assertRaises(ValueError):exec(compile(code,'bound','exec'),{})
    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp)
            (root/'target').write_text('do not read')
            (root/'codex-review.txt').symlink_to(root/'target')
            from unittest.mock import patch
            with patch.dict(os.environ,{'RUNNER_TEMP':tmp}), self.assertRaises(OSError):exec(compile(code,'bound','exec'),{})
    def test_fifo_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.mkfifo(tmp+'/codex-review.txt')
            from unittest.mock import patch
            with patch.dict(os.environ,{'RUNNER_TEMP':tmp}), self.assertRaises(ValueError):exec(compile(code,'bound','exec'),{})
    def test_isolated_python_ignores_cwd_modules(self):
        import subprocess, sys
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp)
            for name in ['uuid', 'pathlib', 'stat']:
                (root/(name+'.py')).write_text("raise RuntimeError('PR module loaded')")
            (root/'codex-review.txt').write_text('safe review')
            env={**os.environ,'RUNNER_TEMP':tmp,'GITHUB_OUTPUT':tmp+'/output','GITHUB_STEP_SUMMARY':tmp+'/summary','PYTHONPATH':tmp}
            result=subprocess.run([sys.executable,'-I','-c',code],cwd=tmp,env=env,capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('safe review',(root/'output').read_text())
if __name__=='__main__': unittest.main()
