/* PDF text is transient in this page, scoped to the next lecture snapshot. */
(function () {
  'use strict';
  const input = document.getElementById('course-pdfs');
  if (!input) return;
  const status = document.getElementById('materials-status');
  const list = document.getElementById('materials-list');
  const clear = document.getElementById('materials-clear');
  let documents = [], busy = false, version = 0, failure = '';
  function render() {
    list.replaceChildren();
    documents.forEach((doc, index) => {
      const row = document.createElement('li');
      const text = document.createElement('span');
      text.textContent = doc.name + ' — ' + doc.pages + ' pages' +
        (doc.pages_without_text ? ' (' + doc.pages_without_text + ' pages have little/no readable text)' : '');
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'secondary'; remove.textContent = 'Remove';
      remove.style.margin = '8px';
      remove.setAttribute('aria-label', 'Remove ' + doc.name);
      remove.addEventListener('click', () => {
        documents.splice(index, 1); render();
        status.textContent = documents.length ? documents.length + ' modules ready for the next lecture.' : 'No modules attached.';
      });
      row.append(text, remove); list.append(row);
    });
    clear.hidden = !documents.length && !failure && !busy;
  }
  clear.addEventListener('click', () => {
    version++; documents = []; failure = ''; busy = false; input.value = '';
    status.textContent = 'Modules cleared. No PDF context will be used for the next lecture.'; render();
  });
  input.addEventListener('change', async () => {
    const generation = ++version;
    documents = []; failure = ''; busy = false; render();
    const files = Array.from(input.files || []);
    if (!files.length) { status.textContent = 'No modules attached.'; return; }
    try {
      if (files.length > 10) throw new Error('Choose at most 10 PDFs.');
      for (const file of files) {
        if (!file.name.toLowerCase().endsWith('.pdf')) throw new Error('Only PDF files are accepted.');
        if (file.size > 10 * 1024 * 1024) throw new Error(file.name + ' exceeds 10 MB.');
        if (!file.size) throw new Error(file.name + ' is empty.');
      }
      if (!BLB.api.session.get() || document.getElementById('legacy')?.checked)
        throw new Error('PDF upload requires lecturer sign-in and the database/API server.');
      busy = true; render();
      status.textContent = 'Processing ' + files.length + ' PDF modules… Please wait before starting the lecture.';
      const form = new FormData();
      files.forEach(file => form.append('files', file));
      const lang = document.getElementById('source-language').value;
      const result = await BLB.api.upload('/materials/analyze?language=' + encodeURIComponent(lang), form);
      if (generation !== version) return;
      documents = result.documents;
      status.textContent = documents.length + ' modules ready. Relevant module passages will support translation automatically.';
    } catch (e) {
      if (generation !== version) return;
      failure = e.message || 'PDF processing failed.'; status.textContent = failure;
    } finally {
      if (generation === version) { busy = false; render(); }
    }
  });
  BLB.courseMaterials = {
    settings() {
      if (busy) throw new Error('Wait for PDF processing to finish before starting the lecture.');
      if (failure) throw new Error('PDF processing failed. Choose valid files or click Clear modules to continue without them.');
      return {materials: documents.map(doc => ({name: doc.name, text: doc.text}))};
    }
  };
})();
