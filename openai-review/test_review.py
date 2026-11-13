import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('review', Path(__file__).with_name('review.py'))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

class ReviewTests(unittest.TestCase):
    def env(self, tmp):
        return dict(GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_REPOSITORY='owner/repo',
                    REVIEW_PR_NUMBER='12', REVIEW_MODEL='example-model', GH_TOKEN='fake-gh',
                    OPENAI_API_KEY='fake-api', RUNNER_TEMP=tmp, GITHUB_OUTPUT=tmp+'/outputs')

    def test_reject_untrusted_events(self):
        for event in ['pull_request', 'pull_request_target', 'issue_comment', 'workflow_run']:
            with self.subTest(event=event), self.assertRaises(r.ReviewError):
                r.config({**self.env('/tmp'), 'GITHUB_EVENT_NAME': event})

    def test_validate_inputs(self):
        for key, value in [('REVIEW_PR_NUMBER','1;evil'), ('REVIEW_MODEL','$(evil)'),
                           ('REVIEW_MAX_OUTPUT_TOKENS','999999'), ('GITHUB_REPOSITORY','a/b/c')]:
            with self.subTest(key=key), self.assertRaises(r.ReviewError):
                r.config({**self.env('/tmp'), key:value})

    def pr(self):
        return dict(state='open', changed_files=1, head={'sha':'a'*40}, base={'sha':'b'*40})

    def test_excludes_sensitive_and_missing_patch(self):
        files=[{'filename':n,'patch':p} for n,p in [('a/.env','secret'),('release.jks','secret'),('x.py','ok'),('img.png',None)]]
        ctx=r.context(self.pr(),files)
        self.assertEqual([p['path'] for p in ctx['patches']],['x.py'])
        self.assertEqual(len(ctx['omitted_files']),3)

    def test_excludes_renamed_sensitive_files(self):
        for old_name in ['.env', 'config/.env.production', 'release.jks', 'secret.PEM']:
            with self.subTest(old_name=old_name):
                ctx = r.context(self.pr(), [{'filename': 'ordinary.txt',
                    'previous_filename': old_name, 'status': 'renamed', 'patch': '-secret'}])
                self.assertFalse(ctx['patches'])
                self.assertEqual(ctx['omitted_files'], ['ordinary.txt'])

    def test_no_tools_no_post_no_pr_execution(self):
        calls=[]
        pr=self.pr()
        def api(url, token, data=None):
            calls.append((url,data))
            if url.endswith('/files?per_page=100'):
                return [{'filename':'dangerous.py','patch':'+ os.system("evil")'}]
            if url.endswith('/responses'):
                self.assertNotIn('tools',data)
                self.assertFalse(data['store'])
                return {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'::error::untrusted'}]}]}
            return pr
        with tempfile.TemporaryDirectory() as tmp, patch.object(r,'api',api):
            dest=r.run(self.env(tmp))
            self.assertIn('::error::untrusted',dest.read_text())
        self.assertEqual(len(calls),4)
        self.assertEqual(sum(data is not None for _,data in calls),1)

    def test_changed_sha_prevents_model_spend(self):
        pr=self.pr(); moved={**pr,'head':{'sha':'c'*40}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(r,'api',side_effect=[pr,[{'filename':'x','patch':'+x'}],moved]) as mock:
            with self.assertRaises(r.ReviewError):r.run(self.env(tmp))
            self.assertEqual(mock.call_count,3)

    def test_large_pr_prevents_spend(self):
        with patch.object(r,'api',return_value={**self.pr(),'changed_files':101}):
            with self.assertRaises(r.ReviewError):r.run(self.env('/tmp'))

    def test_context_cap(self):
        ctx=r.context(self.pr(),[{'filename':'large','patch':'x'*(r.MAX_CONTEXT+1)}])
        self.assertFalse(ctx['patches'])

    def test_redirect_rejected(self):
        with self.assertRaises(r.ReviewError):
            r.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.example')

if __name__=='__main__':unittest.main()
