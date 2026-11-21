import { MAX_LIGHT_REGIONS } from './emission.js';

const PALETTE = ['#4bd4ff', '#ff647c', '#9b82ff', '#6df4a9', '#ffb35a', '#fbdcff', '#e9f96d', '#ffffff'];

function canvas(width, height) {
  const element = document.createElement('canvas');
  element.width = width; element.height = height;
  return element;
}

async function decode(source) {
  const image = new Image();
  image.src = source;
  await image.decode();
  return image;
}

function swatch(color) {
  const chip = document.createElement('span');
  chip.className = 'light-swatch';
  chip.style.backgroundColor = color;
  return chip;
}

export class LightRegionEditor {
  constructor(dialog, { saveMask, apply, onOpen, onClose, onError }) {
    this.dialog = dialog;
    this.saveMask = saveMask; this.apply = apply; this.onError = onError;
    this.onOpen = onOpen; this.onClose = onClose;
    this.display = this.el('mask-canvas');
    this.stage = this.el('mask-stage');
    new ResizeObserver(() => this.fit()).observe(this.stage);
    for (const button of dialog.querySelectorAll('[data-mask-panel]')) {
      button.addEventListener('click', () => this.showPanel(button.dataset.maskPanel));
    }
    this.connect('mask-add-region', 'click', () => {
      if (this.regions.length >= MAX_LIGHT_REGIONS) return;
      const region = this.makeRegion();
      this.regions.push(region);
      this.select(region);
    });
    this.connect('mask-remove-region', 'click', () => {
      if (this.regions.length < 2) return;
      const index = this.regions.indexOf(this.region);
      this.history = this.history.filter(entry => entry.region !== this.region);
      this.regions.splice(index, 1);
      this.select(this.regions[Math.min(index, this.regions.length - 1)]);
    });
    this.connect('mask-region-name', 'change', event => {
      this.region.name = event.target.value.trim() || '发光区域';
      this.select(this.region);
    });
    this.connect('mask-region-color', 'input', event => {
      this.region.color = event.target.value;
      this.el('mask-regions').querySelector('.active .light-swatch').style.backgroundColor = this.region.color;
      this.draw();
    });
    this.connect('mask-original-color', 'change', event => {
      this.region.useImageColor = event.target.checked;
      this.draw();
    });
    this.connect('mask-original-view', 'change', () => this.draw());
    for (const button of dialog.querySelectorAll('[data-mask-tool]')) button.addEventListener('click', () => {
      this.tool = button.dataset.maskTool;
      for (const item of dialog.querySelectorAll('[data-mask-tool]')) item.classList.toggle('active', button === item);
    });
    this.connect('mask-undo', 'click', () => {
      const previous = this.history.pop();
      if (!previous) return;
      previous.region.image.getContext('2d').putImageData(previous.pixels, 0, 0);
      previous.region.dirty = previous.dirty;
      this.select(previous.region);
    });
    for (const [id, fill] of [['mask-fill', true], ['mask-clear', false]]) this.connect(id, 'click', () => {
      this.remember();
      const context = this.region.image.getContext('2d');
      context.clearRect(0, 0, this.width, this.height);
      if (fill) {
        context.fillStyle = 'white';
        context.fillRect(0, 0, this.width, this.height);
      }
      this.draw();
    });
    this.input = document.querySelector('#mask-input');
    this.connect('import-mask', 'click', () => this.input.click());
    this.input.addEventListener('change', async () => {
      const file = this.input.files[0];
      this.input.value = '';
      if (!file || !this.dialog.open) return;
      const target = this.region;
      const url = URL.createObjectURL(file);
      try {
        const image = await decode(url);
        if (!this.dialog.open || !this.regions.includes(target)) return;
        if (image.width !== this.artwork.width || image.height !== this.artwork.height) throw new Error('导入蒙版的尺寸需与原图一致');
        this.remember(target);
        this.readMask(image, target.image);
        this.draw();
      } catch (error) {
        this.onError(error);
      } finally {
        URL.revokeObjectURL(url);
      }
    });
    this.connect('save-mask', 'click', () => this.save());
    this.display.addEventListener('pointerdown', event => this.startStroke(event));
    this.display.addEventListener('pointermove', event => {
      if (event.pointerId !== this.pointer) return;
      const point = this.position(event);
      if (this.tool === 'lasso') this.points.push(point);
      else this.stroke(point);
      this.draw();
    });
    this.display.addEventListener('pointerup', event => this.endStroke(event));
    this.display.addEventListener('pointercancel', event => this.endStroke(event, true));
    this.display.addEventListener('lostpointercapture', event => this.endStroke(event, true));
    dialog.addEventListener('close', () => {
      this.pointer = null;
      this.points = [];
      this.regions = [];
      this.history = [];
      this.region = this.artwork = this.scratch = null;
      this.onClose();
    });
    dialog.addEventListener('cancel', event => {
      if (this.saving) event.preventDefault();
    });
  }

  el(id) { return this.dialog.querySelector('#' + id); }

  connect(id, event, handler) {
    this.el(id).addEventListener(event, async input => {
      try { await handler(input); } catch (error) { this.onError(error); }
    });
  }

  async open(layer, settings) {
    if (this.opening || this.dialog.open) return;
    this.opening = true;
    try {
      this.layerId = layer.layerId; this.layerName = layer.name;
      this.settings = settings;
      this.artwork = await decode(`/images/${layer.imageHash}.png`);
      const scale = Math.min(1, 1600 / Math.max(this.artwork.width, this.artwork.height));
      this.width = Math.max(1, Math.round(this.artwork.width * scale));
      this.height = Math.max(1, Math.round(this.artwork.height * scale));
      this.display.width = this.width; this.display.height = this.height;
      this.scratch = canvas(this.width, this.height);
      const light = layer.emission || {};
      const previous = light.regions || (light.maskHash || light.enabled ? [{
        name: '发光区域 1', color: light.color || '#ffffff', maskHash: light.maskHash, useImageColor: true
      }] : []);
      this.regions = [];
      for (const entry of previous) {
        const region = this.makeRegion(entry);
        if (entry.maskHash) this.readMask(await decode(`/images/${entry.maskHash}.png`), region.image);
        else {
          const context = region.image.getContext('2d');
          context.fillStyle = 'white';
          context.fillRect(0, 0, this.width, this.height);
        }
        this.regions.push(region);
      }
      if (!this.regions.length) this.regions.push(this.makeRegion());
      this.history = [];
      this.points = [];
      this.tool = 'paint';
      for (const item of this.dialog.querySelectorAll('[data-mask-tool]')) item.classList.toggle('active', item.dataset.maskTool === this.tool);
      this.el('mask-original-view').checked = false;
      this.el('mask-title').textContent = '图片发光区域 · ' + layer.name;
      this.select(this.regions[0]);
      this.showPanel('regions');
      this.dialog.showModal();
      this.fit();
      this.onOpen();
    } finally {
      this.opening = false;
    }
  }

  showPanel(panel) {
    this.panel = panel;
    for (const button of this.dialog.querySelectorAll('[data-mask-panel]')) {
      button.setAttribute('aria-pressed', String(button.dataset.maskPanel === panel));
    }
    this.el('mask-region-settings').hidden = panel !== 'regions';
    this.el('mask-effect-settings').hidden = panel !== 'effect';
    this.dialog.querySelector('.mask-toolbar').hidden = panel !== 'regions';
  }

  fit() {
    if (!this.dialog.open) return;
    const scale = Math.max(.01, Math.min((this.stage.clientWidth - 24) / this.width, (this.stage.clientHeight - 24) / this.height));
    this.display.style.width = `${Math.floor(this.width * scale)}px`;
    this.display.style.height = `${Math.floor(this.height * scale)}px`;
  }

  makeRegion(entry = {}) {
    const number = this.regions.length;
    return { name: `发光区域 ${number + 1}`, color: PALETTE[number], useImageColor: false,
      ...entry, image: canvas(this.width, this.height), dirty: false };
  }

  readMask(image, target) {
    const context = target.getContext('2d', { willReadFrequently: true });
    context.clearRect(0, 0, this.width, this.height);
    context.drawImage(image, 0, 0, this.width, this.height);
    const pixels = context.getImageData(0, 0, this.width, this.height);
    for (let index = 0; index < pixels.data.length; index += 4) {
      pixels.data[index + 3] = Math.round(pixels.data[index] * pixels.data[index + 3] / 255);
      pixels.data[index] = pixels.data[index + 1] = pixels.data[index + 2] = 255;
    }
    context.putImageData(pixels, 0, 0);
  }

  select(region) {
    this.region = region;
    const list = this.el('mask-regions');
    list.replaceChildren();
    for (const entry of this.regions) {
      const button = document.createElement('button');
      const active = region === entry;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
      button.append(swatch(entry.color), entry.name);
      button.addEventListener('click', () => this.select(entry));
      list.append(button);
    }
    this.el('mask-region-name').value = region.name;
    this.el('mask-region-color').value = region.color;
    this.el('mask-original-color').checked = region.useImageColor;
    this.el('mask-add-region').disabled = this.regions.length >= MAX_LIGHT_REGIONS;
    this.el('mask-remove-region').disabled = this.regions.length <= 1;
    this.el('mask-status').textContent = '';
    this.draw();
  }

  draw() {
    const context = this.display.getContext('2d');
    context.clearRect(0, 0, this.width, this.height);
    context.drawImage(this.artwork, 0, 0, this.width, this.height);
    if (!this.el('mask-original-view').checked) {
      const scratch = this.scratch.getContext('2d');
      context.globalAlpha = .85;
      for (const region of this.regions) {
        scratch.clearRect(0, 0, this.width, this.height);
        scratch.drawImage(region.image, 0, 0);
        scratch.globalCompositeOperation = 'source-in';
        scratch.fillStyle = region.color;
        scratch.fillRect(0, 0, this.width, this.height);
        if (region.useImageColor) {
          scratch.globalCompositeOperation = 'multiply';
          scratch.drawImage(this.artwork, 0, 0, this.width, this.height);
          scratch.globalCompositeOperation = 'destination-in';
          scratch.drawImage(region.image, 0, 0);
        }
        scratch.globalCompositeOperation = 'destination-in';
        scratch.drawImage(this.artwork, 0, 0, this.width, this.height);
        scratch.globalCompositeOperation = 'source-over';
        context.drawImage(this.scratch, 0, 0);
      }
      context.globalAlpha = 1;
    }
    if (this.tool === 'lasso' && this.points.length) {
      this.path(context);
      context.strokeStyle = this.region.color;
      context.lineWidth = Math.max(1, Math.max(this.width, this.height) / 480);
      context.stroke();
    }
  }

  position(event) {
    const rect = this.display.getBoundingClientRect();
    return { x: (event.clientX - rect.left) / rect.width * this.width,
      y: (event.clientY - rect.top) / rect.height * this.height };
  }

  remember(region = this.region) {
    const pixels = region.image.getContext('2d').getImageData(0, 0, this.width, this.height);
    this.history.push({ region, pixels, dirty: region.dirty });
    const limit = Math.max(1, Math.min(15, Math.floor(96 * 1024 * 1024 / pixels.data.byteLength)));
    while (this.history.length > limit) this.history.shift();
    region.dirty = true;
  }

  startStroke(event) {
    if (event.button !== 0 || this.pointer != null || this.saving || this.panel !== 'regions') return;
    this.remember();
    this.pointer = event.pointerId;
    this.display.setPointerCapture(this.pointer);
    this.last = null;
    this.points = [this.position(event)];
    this.el('mask-original-view').checked = false;
    if (this.tool !== 'lasso') this.stroke(this.points[0]);
    this.draw();
  }

  path(context) {
    context.beginPath();
    this.points.forEach((point, index) => index ? context.lineTo(point.x, point.y) : context.moveTo(point.x, point.y));
  }

  stroke(point) {
    const context = this.region.image.getContext('2d');
    context.globalCompositeOperation = this.tool === 'erase' ? 'destination-out' : 'source-over';
    context.beginPath();
    const width = Math.max(1, Number(this.el('mask-size').value) * Math.max(this.width, this.height) / 600);
    if (this.last) {
      context.moveTo(this.last.x, this.last.y);
      context.lineTo(point.x, point.y);
      context.strokeStyle = 'white'; context.lineWidth = width; context.lineCap = 'round';
      context.stroke();
    } else {
      context.arc(point.x, point.y, width / 2, 0, 2 * Math.PI);
      context.fillStyle = 'white';
      context.fill();
    }
    context.globalCompositeOperation = 'source-over';
    this.last = point;
  }

  endStroke(event, cancelled = false) {
    if (event.pointerId !== this.pointer) return;
    if (!cancelled && this.tool === 'lasso' && this.points.length > 2) {
      const context = this.region.image.getContext('2d');
      this.path(context);
      context.closePath();
      context.fillStyle = 'white';
      context.fill();
    }
    this.pointer = null;
    this.points = [];
    this.draw();
  }

  async save() {
    if (this.saving) return;
    this.saving = true;
    const button = this.el('save-mask');
    const close = this.dialog.querySelector('form button');
    button.disabled = close.disabled = true;
    this.el('mask-status').textContent = '正在保存发光区域…';
    try {
      const regions = [];
      for (const { name, color, useImageColor, maskHash, dirty, image } of this.regions) {
        const hash = maskHash && !dirty ? maskHash : await this.saveMask(this.layerName + ' · ' + name, image.toDataURL('image/png'));
        regions.push({ name, color, useImageColor, maskHash: hash });
      }
      this.apply(this.layerId, regions, this.settings);
      this.dialog.close();
    } finally {
      button.disabled = close.disabled = false;
      this.saving = false;
      if (this.dialog.open) this.select(this.region);
    }
  }
}
