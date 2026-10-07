// Native multipart upload remains usable when JavaScript is unavailable.
document.querySelectorAll('[data-photo-editor]').forEach(editor => {
  const input = editor.querySelector('[data-photo-input]');
  const previews = editor.querySelector('[data-photo-previews]');
  const message = editor.querySelector('[data-photo-message]');
  const form = editor.closest('form');
  const maxBytes = Number(editor.dataset.requestLimit || 0);
  let selected = [];
  let objectUrls = [];
  let editableFiles = false;
  try {
    input.files = new DataTransfer().files;
    editableFiles = true;
  } catch (_) {
    /* Native fallback */
  }

  editor.querySelectorAll('[data-existing-photo]').forEach(card => {
    const check = card.querySelector('[name=keep_gallery]');
    const note = card.querySelector('.photo-remove-note');
    const update = () => {
      card.classList.toggle('is-removed', !check.checked);
      note.hidden = check.checked;
    };
    check.addEventListener('change', update);
    update();
  });

  function syncFiles() {
    if (!editableFiles) return;
    const transfer = new DataTransfer();
    selected.forEach(file => transfer.items.add(file));
    input.files = transfer.files;
  }

  function validateSelection() {
    const allFiles = [...form.querySelectorAll('input[type=file]')]
      .flatMap(element => [...element.files]);
    const total = allFiles.reduce((sum, file) => sum + file.size, 0);
    let problem = '';
    if (selected.length > 20) problem = '每次最多新增 20 张，请移除部分照片后分次保存。';
    else if (maxBytes && total >= maxBytes) problem = '所选文件的总大小达到或超过提交上限，请减少照片或压缩后重试。';
    input.setCustomValidity(problem);
    message.classList.toggle('is-error', Boolean(problem));
    message.textContent = problem || (selected.length ?
      `已选择 ${selected.length} 张新照片；本次所有文件共 ${(total / 1024 / 1024).toFixed(2)} MB。保存演出后才会上传，表单文字也计入总上限。` :
      '尚未选择新照片。');
    if (!editableFiles) message.textContent += ' 此浏览器不支持逐张移除待上传文件，请重新选择整批照片。';
  }

  function render() {
    objectUrls.forEach(url => URL.revokeObjectURL(url));
    objectUrls = [];
    previews.replaceChildren();
    selected.forEach((file, index) => {
      const card = document.createElement('article');
      card.className = 'photo-card';
      const img = document.createElement('img');
      const objectUrl = URL.createObjectURL(file);
      objectUrls.push(objectUrl);
      img.src = objectUrl;
      img.alt = `待上传照片 ${index + 1}`;
      const caption = document.createElement('p');
      caption.className = 'photo-name';
      caption.textContent = file.name;
      card.append(img, caption);
      if (editableFiles) {
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'photo-remove';
        remove.textContent = '移除';
        remove.setAttribute('aria-label', `移除待上传照片 ${index + 1}`);
        remove.addEventListener('click', () => {
          selected.splice(index, 1);
          syncFiles();
          render();
          const next = previews.querySelectorAll('button')[Math.min(index, selected.length - 1)];
          (next || input).focus();
        });
        card.append(remove);
      }
      previews.append(card);
    });
    validateSelection();
  }

  input.addEventListener('change', () => {
    const added = [...input.files];
    if (editableFiles) {
      added.forEach(file => {
        if (!selected.some(old => old.name === file.name && old.size === file.size && old.lastModified === file.lastModified)) selected.push(file);
      });
      syncFiles();
    } else selected = added;
    render();
  });
  form.querySelectorAll('input[type=file]').forEach(element => {
    if (element !== input) element.addEventListener('change', validateSelection);
  });
  form.addEventListener('submit', event => {
    validateSelection();
    if (!input.checkValidity()) {
      event.preventDefault();
      input.reportValidity();
    }
  });
  window.addEventListener('pagehide', () => {
    objectUrls.forEach(url => URL.revokeObjectURL(url));
    objectUrls = [];
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted) render();
  });
});
