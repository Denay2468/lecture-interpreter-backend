"use strict";
// NODE_PATH=<directory containing jsdom> node frontend/tests/course-profiles.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM} = require('jsdom');
const root = path.resolve(__dirname, '..');
const code = fs.readFileSync(path.join(root,'js/course-profiles.js'),'utf8');
const catalog = JSON.parse(fs.readFileSync(path.join(root,'data/it-courses.json'),'utf8'));
async function page(fail=false) {
  const dom = new JSDOM('<select id="source-language"><option value="id">ID</option><option value="ja">JA</option></select><select id="course-profile"><option value="">Custom</option></select><p id="course-profile-note"></p><pre id="course-preview"></pre><textarea id="course-glossary"></textarea>', {runScripts:'outside-only'});
  dom.window.BLB = {};
  dom.window.fetch = async () => ({ok:!fail, json: async () => catalog});
  dom.window.eval(code);
  dom.window.BLB.courseProfiles.init();
  await new Promise(r => setTimeout(r, 0));
  return dom;
}
(async () => {
  const d=await page(), w=d.window, get=id=>w.document.getElementById(id);
  const change=id=>get(id).dispatchEvent(new w.Event('change'));
  assert.equal(get('course-profile').options.length, 7);
  get('course-profile').value='networks';change('course-profile');
  assert(get('course-preview').textContent.includes('kehilangan paket = packet loss'));
  get('course-glossary').value='jaringan = LAN';
  assert.deepEqual(JSON.parse(JSON.stringify(w.BLB.courseProfiles.settings())),{course_profile:'networks',glossary:'jaringan = LAN'});
  get('source-language').value='ja';change('source-language');
  assert.equal(get('course-profile').value,'');assert(get('course-profile').disabled);
  assert.equal(get('course-glossary').value,'jaringan = LAN');
  get('source-language').value='id';change('source-language');
  assert(!get('course-profile').disabled);
  get('course-profile').value='networks';change('course-profile');
  get('course-glossary').value=Array.from({length:17},(_,i)=>`extra${i}=term${i}`).join('\n');
  assert.throws(()=>w.BLB.courseProfiles.settings(), /40 terms/);
  get('course-glossary').value='abc=x\nABC=y';
  assert.throws(()=>w.BLB.courseProfiles.settings(), /Duplicate/);
  get('course-glossary').value='missing equals';
  assert.throws(()=>w.BLB.courseProfiles.settings(), /source term/);
  const failed=await page(true);
  assert(failed.window.document.getElementById('course-profile').disabled);
  assert.equal(failed.window.BLB.courseProfiles.settings().course_profile,'');
  d.window.close();failed.window.close();
  console.log('PASS: six courses, preview, payload, language guard, override preservation, limits, duplicates, invalid syntax, catalog failure fallback');
})().catch(e=>{console.error(e);process.exit(1);});
