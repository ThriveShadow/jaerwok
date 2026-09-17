(() => {
  let projectId = null;
  let pendingFiles = [];
  let pollTimer = null;

  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const fileList = document.getElementById("file-list");
  const uploadCount = document.getElementById("upload-count");
  const btnUpload = document.getElementById("btn-upload");
  const btnToStep2 = document.getElementById("btn-to-step2");
  const btnStartProcess = document.getElementById("btn-start-process");
  const uploadSection = document.getElementById("upload-section");
  const btnNewProject = document.getElementById("btn-new-project");

  const progressWrap = document.getElementById("progress-wrap");
  const progressFill = document.getElementById("progress-fill");
  const progressLabel = document.getElementById("progress-label");
  const jobLog = document.getElementById("job-log");
  const resultWrap = document.getElementById("result-wrap");
  const errorWrap = document.getElementById("error-wrap");

  const optYoloModel = document.getElementById("opt-yolo-model");
  const btnUploadModel = document.getElementById("btn-upload-model");
  const modelFileInput = document.getElementById("model-file-input");
  const btnStartYolo = document.getElementById("btn-start-yolo");
  const yoloProgressWrap = document.getElementById("yolo-progress-wrap");
  const yoloProgressFill = document.getElementById("yolo-progress-fill");
  const yoloProgressLabel = document.getElementById("yolo-progress-label");
  const yoloJobLog = document.getElementById("yolo-job-log");
  const yoloResultWrap = document.getElementById("yolo-result-wrap");
  const yoloErrorWrap = document.getElementById("yolo-error-wrap");
  let yoloPollTimer = null;

  // ---- Confirm modal ---------------------------------------------------
  const confirmModal = document.getElementById("confirm-modal");
  const confirmMessage = document.getElementById("confirm-modal-message");
  const confirmOk = document.getElementById("confirm-modal-ok");
  const confirmCancel = document.getElementById("confirm-modal-cancel");

  function askConfirm(message) {
    return new Promise((resolve) => {
      confirmMessage.textContent = message;
      confirmModal.classList.remove("hidden");

      function cleanup(result) {
        confirmModal.classList.add("hidden");
        confirmOk.removeEventListener("click", onOk);
        confirmCancel.removeEventListener("click", onCancel);
        resolve(result);
      }
      function onOk() { cleanup(true); }
      function onCancel() { cleanup(false); }

      confirmOk.addEventListener("click", onOk);
      confirmCancel.addEventListener("click", onCancel);
    });
  }

  // ---- Existing projects (with delete) ---------------------------------
  async function loadExistingProjects() {
    const res = await fetch("/api/projects");
    const data = await res.json();
    const grid = document.getElementById("projects-grid");
    grid.innerHTML = "";

    if (data.projects.length === 0) {
      grid.innerHTML = "<p class='hint'>No existing projects found.</p>";
      return;
    }

    data.projects.forEach(p => {
      const card = document.createElement("div");
      card.style = "border: 1px solid var(--border); border-radius: 8px; padding: 10px; cursor: pointer;";
      const imgHtml = p.has_preview
        ? `<img src="${p.preview_url}" style="width: 100%; height: 120px; object-fit: cover; border-radius: 4px;">`
        : `<div style="width: 100%; height: 120px; background: #eee; display: flex; align-items: center; justify-content: center;">No Preview</div>`;

      card.innerHTML = `${imgHtml}<p style="margin: 8px 0 0; font-size: 0.85rem; font-weight: bold; text-align: center;">${p.id}</p>`;

      card.addEventListener("click", () => {
        openProject(p.id);
      });

      const delBtn = document.createElement("button");
      delBtn.className = "danger project-delete-btn";
      delBtn.textContent = "Delete";
      delBtn.addEventListener("click", async (e) => {
        e.stopPropagation();

        const pwd = window.prompt("Enter admin password to delete this project:");
        if (pwd !== "musangking") {
          alert("Incorrect password. Deletion cancelled.");
          return; // Stop the function here
        }

        const ok = await askConfirm(`Delete project "${p.id}" and all its files? This cannot be undone.`);
        if (!ok) return;
        try {
          const res = await fetch(`/api/project/${p.id}`, { method: "DELETE" });
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || "Failed to delete");
          loadExistingProjects();
        } catch (err) {
          alert(`Error: ${err.message}`);
        }
      });
      card.appendChild(delBtn);

      grid.appendChild(card);
    });
  }

  loadExistingProjects();

  function resetProjectState() {
    projectId = null;
    pendingFiles = [];
    renderFileList();
    uploadProgressWrap.classList.add("hidden");
    uploadProgressFill.style.width = "0%";
    uploadProgressLabel.textContent = "0%";
    progressWrap.classList.add("hidden");
    resultWrap.classList.add("hidden");
    errorWrap.classList.add("hidden");
    btnToStep2.disabled = true;
    uploadCount.textContent = "0 images uploaded";
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }

    ngrdiResultWrap.classList.add("hidden");
    ngrdiProgressWrap.classList.add("hidden");
    ngrdiErrorWrap.classList.add("hidden");
    if (ngrdiPollTimer) { clearInterval(ngrdiPollTimer); ngrdiPollTimer = null; }

    yoloResultWrap.classList.add("hidden");
    yoloProgressWrap.classList.add("hidden");
    yoloErrorWrap.classList.add("hidden");
    if (yoloPollTimer) { clearInterval(yoloPollTimer); yoloPollTimer = null; }

    reportResultWrap.classList.add("hidden");
    reportErrorWrap.classList.add("hidden");
  }

  btnNewProject.addEventListener("click", () => {
    resetProjectState();
    uploadSection.classList.remove("hidden");
    goToStep(1);
  });

  // ---- Re-sync all step states when opening/reopening a project --------
  async function openProject(pid) {
    projectId = pid;
    goToStep(2);

    await Promise.all([
      resumeStep2(pid),
      resumeStep3(pid),
      resumeStep4(pid),
    ]);
  }

  async function resumeStep2(pid) {
    try {
      const statusRes = await fetch(`/api/status/${pid}`);
      if (statusRes.ok) {
        const job = await statusRes.json();
        if (job.status === "running") {
          errorWrap.classList.add("hidden");
          resultWrap.classList.add("hidden");
          progressWrap.classList.remove("hidden");
          progressFill.style.width = `${job.progress || 0}%`;
          progressLabel.textContent = job.log.length ? job.log[job.log.length - 1] : "Working...";
          jobLog.textContent = job.log.join("\n");
          btnStartProcess.disabled = true;
          if (pollTimer) clearInterval(pollTimer);
          pollTimer = setInterval(pollStatus, 2000);
          return;
        }
        if (job.status === "error") {
          progressWrap.classList.add("hidden");
          showError(job.error || "Reconstruction failed.");
          return;
        }
        // status === "done" falls through to loadResult() below
      }
    } catch (e) {
      // no job record at all (never started, or server restarted) — fall through
    }
    await loadResult();
  }

  async function resumeStep3(pid) {
    try {
      const statusRes = await fetch(`/api/ngrdi/status/${pid}`);
      if (statusRes.ok) {
        const job = await statusRes.json();
        if (job.status === "running") {
          ngrdiErrorWrap.classList.add("hidden");
          ngrdiResultWrap.classList.add("hidden");
          ngrdiProgressWrap.classList.remove("hidden");
          ngrdiProgressFill.style.width = `${job.progress || 0}%`;
          ngrdiProgressLabel.textContent = job.log.length ? job.log[job.log.length - 1] : "Working...";
          ngrdiJobLog.textContent = job.log.join("\n");
          btnStartNgrdi.disabled = true;
          if (ngrdiPollTimer) clearInterval(ngrdiPollTimer);
          ngrdiPollTimer = setInterval(pollNgrdiStatus, 2000);
          return;
        }
        if (job.status === "error") {
          ngrdiProgressWrap.classList.add("hidden");
          showNgrdiError(job.error || "NGRDI analysis failed.");
          return;
        }
        if (job.status === "done") {
          await loadNgrdiResult();
          return;
        }
      }
    } catch (e) {
      // no job record — leave step 3 at its default empty state
    }
  }

  async function resumeStep4(pid) {
    try {
      const statusRes = await fetch(`/api/yolo/status/${pid}`);
      if (statusRes.ok) {
        const job = await statusRes.json();
        if (job.status === "running") {
          yoloErrorWrap.classList.add("hidden");
          yoloResultWrap.classList.add("hidden");
          yoloProgressWrap.classList.remove("hidden");
          yoloProgressFill.style.width = `${job.progress || 0}%`;
          yoloProgressLabel.textContent = job.log.length ? job.log[job.log.length - 1] : "Working...";
          yoloJobLog.textContent = job.log.join("\n");
          btnStartYolo.disabled = true;
          if (yoloPollTimer) clearInterval(yoloPollTimer);
          yoloPollTimer = setInterval(pollYoloStatus, 2000);
          return;
        }
        if (job.status === "error") {
          yoloProgressWrap.classList.add("hidden");
          showYoloError(job.error || "Detection failed.");
          return;
        }
        if (job.status === "done") {
          await loadYoloResult();
          return;
        }
      }
    } catch (e) {
      // no job record — leave step 4 at its default empty state
    }
  }

  async function autoFetchReport() {
  if (!projectId) return;

  reportErrorWrap.classList.add("hidden");
  reportResultWrap.classList.add("hidden");
  reportProgressLabel.classList.remove("hidden");

  try {
    const res = await fetch(`/api/report/generate/${projectId}`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to generate report");
    await loadFullReport();
  } catch (err) {
    showReportError(err.message);
  } finally {
    reportProgressLabel.classList.add("hidden");
  }
  }

  // ---- Step navigation ----------------------------------------------
  function goToStep(n) {
    document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
    document.getElementById(`panel-${n}`).classList.add("active");
    document.querySelectorAll(".step").forEach(s => {
      s.classList.toggle("active", Number(s.dataset.step) === n);
    });

    if (n === 5) {
      autoFetchReport();
    }
  }

  document.querySelectorAll(".step:not(.disabled)").forEach(el => {
    el.addEventListener("click", () => goToStep(Number(el.dataset.step)));
  });

  // ---- Step 1: file selection ----------------------------------------
  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("dragover", e => { e.preventDefault(); dropzone.classList.add("dragover"); });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
  dropzone.addEventListener("drop", e => {
    e.preventDefault();
    dropzone.classList.remove("dragover");
    addFiles(e.dataTransfer.files);
  });
  fileInput.addEventListener("change", () => addFiles(fileInput.files));

  function addFiles(fileListObj) {
    for (const f of fileListObj) pendingFiles.push(f);
    renderFileList();
  }

  function renderFileList() {
    fileList.innerHTML = "";
    pendingFiles.forEach((f, i) => {
      const li = document.createElement("li");
      li.innerHTML = `<span>${f.name}</span><span>${(f.size / 1024 / 1024).toFixed(1)} MB</span>`;
      fileList.appendChild(li);
    });
    uploadCount.textContent = `${pendingFiles.length} images selected`;
    btnUpload.disabled = pendingFiles.length === 0;
  }

  const uploadProgressWrap = document.getElementById("upload-progress-wrap");
  const uploadProgressFill = document.getElementById("upload-progress-fill");
  const uploadProgressLabel = document.getElementById("upload-progress-label");

  let currentUploadFiles = [];
  let totalBytes = 0;

  function updateProgress() {
    const loaded = currentUploadFiles.reduce((s, f) => s + (f._loaded || 0), 0);
    const pct = totalBytes ? Math.round((loaded / totalBytes) * 100) : 0;
    uploadProgressFill.style.width = `${pct}%`;
    uploadProgressLabel.textContent =
      `${pct}% (${(loaded / 1024 / 1024).toFixed(1)} / ${(totalBytes / 1024 / 1024).toFixed(1)} MB)`;
  }

  function uploadSingleFile(file, pid) {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      if (pid) form.append("project_id", pid);
      form.append("images", file);

      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");

      xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) {
          file._loaded = e.loaded;
          updateProgress();
        }
      });

      xhr.onload = () => {
        file._loaded = file.size;
        updateProgress();
        try {
          const data = JSON.parse(xhr.responseText);
          if (xhr.status >= 200 && xhr.status < 300) resolve(data);
          else reject(new Error(data.error || `HTTP ${xhr.status}`));
        } catch {
          reject(new Error(`bad response (HTTP ${xhr.status})`));
        }
      };

      xhr.onerror = () => reject(new Error("Network error mid-upload"));
      xhr.send(form);
    });
  }

  function runWithConcurrency(items, limit, workerFn) {
    return new Promise((resolve, reject) => {
      const results = new Array(items.length);
      let nextIndex = 0;
      let completed = 0;
      let failed = false;

      function startNext() {
        if (failed) return;
        const i = nextIndex++;
        if (i >= items.length) return;

        workerFn(items[i], i)
          .then(result => {
            results[i] = result;
            completed++;
            if (completed === items.length) resolve(results);
            else startNext();
          })
          .catch(err => {
            failed = true;
            reject(err);
          });
      }

      const workers = Math.min(limit, items.length);
      for (let i = 0; i < workers; i++) startNext();
    });
  }

  btnUpload.addEventListener("click", async () => {
    if (pendingFiles.length === 0) return;
    btnUpload.disabled = true;
    btnUpload.textContent = "Uploading...";
    uploadProgressWrap.classList.remove("hidden");

    currentUploadFiles = pendingFiles.slice();
    currentUploadFiles.forEach(f => f._loaded = 0);
    totalBytes = currentUploadFiles.reduce((s, f) => s + f.size, 0);
    updateProgress();

    try {
      let totalSaved = 0;
      let skipped = [];

      const first = await uploadSingleFile(currentUploadFiles[0], projectId);
      projectId = first.project_id;
      totalSaved += first.saved_count || 0;
      if (first.skipped) skipped.push(...first.skipped);

      const rest = currentUploadFiles.slice(1);
      if (rest.length > 0) {
        const results = await runWithConcurrency(rest, 3, (file) => uploadSingleFile(file, projectId));
        results.forEach(r => {
          totalSaved += r.saved_count || 0;
          if (r.skipped) skipped.push(...r.skipped);
        });
      }

      const listRes = await fetch(`/api/project/${projectId}/images`);
      const listData = await listRes.json();
      uploadCount.textContent = `${listData.count} images uploaded`;
      btnToStep2.disabled = listData.count < 3;

      pendingFiles = [];
      renderFileList();
      uploadProgressLabel.textContent = `Done — ${totalSaved} files uploaded`;
      if (skipped.length) alert(`Skipped unsupported files: ${skipped.join(", ")}`);

    } catch (err) {
      alert(`Upload failed: ${err.message}`);
    } finally {
      btnUpload.disabled = pendingFiles.length === 0;
      btnUpload.textContent = "Upload selected files";
    }
  });

  btnToStep2.addEventListener("click", () => goToStep(2));

  const btnRegen = document.getElementById("btn-regen-report");
  if (btnRegen) {
    btnRegen.addEventListener("click", async () => {
      if (!projectId) return;
      btnRegen.textContent = "Refreshing...";
      btnRegen.disabled = true;
      try {
        const res = await fetch(`/api/report/regenerate/${projectId}`, { method: "POST" });
        if (!res.ok) throw new Error("Failed to refresh summary");
        await loadResult();
      } catch (err) {
        alert(`Error: ${err.message}`);
      } finally {
        btnRegen.textContent = "Refresh Summary";
        btnRegen.disabled = false;
      }
    });
  }

  // ---- Step 2: run reconstruction -------------------------------------
  btnStartProcess.addEventListener("click", async () => {
    if (!projectId) { alert("Upload images first."); return; }
    errorWrap.classList.add("hidden");
    resultWrap.classList.add("hidden");
    progressWrap.classList.remove("hidden");
    progressFill.style.width = "1%";
    progressLabel.textContent = "Starting...";
    jobLog.textContent = "";
    btnStartProcess.disabled = true;

    const opts = {
      compute: document.getElementById("opt-compute").value,
      quality: document.getElementById("opt-quality").value,
      max_concurrency: Number(document.getElementById("opt-concurrency").value),
    };

    try {
      const res = await fetch(`/api/process/${projectId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(opts),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "failed to start");
      pollTimer = setInterval(pollStatus, 2000);
    } catch (err) {
      showError(err.message);
      btnStartProcess.disabled = false;
    }
  });

  async function pollStatus() {
    const res = await fetch(`/api/status/${projectId}`);
    const job = await res.json();
    if (!res.ok) return;

    progressFill.style.width = `${job.progress || 0}%`;
    progressLabel.textContent = job.log.length ? job.log[job.log.length - 1] : "Working...";
    jobLog.textContent = job.log.join("\n");
    jobLog.scrollTop = jobLog.scrollHeight;

    if (job.status === "done") {
      clearInterval(pollTimer);
      btnStartProcess.disabled = false;
      await loadResult();
    } else if (job.status === "error") {
      clearInterval(pollTimer);
      btnStartProcess.disabled = false;
      showError(job.error || "Reconstruction failed.");
    }
  }

  async function loadResult() {
    const res = await fetch(`/api/result/${projectId}`);
    const data = await res.json();
    if (!res.ok) { showError(data.error || "Could not load result."); return; }

    resultWrap.classList.remove("hidden");
    const img = document.getElementById("ortho-preview");
    const dl = document.getElementById("ortho-download");

    if (data.preview_available) {
      img.src = data.preview_url + `?t=${Date.now()}`;
      img.classList.remove("hidden");
    } else {
      img.classList.add("hidden");
    }
    dl.href = data.orthophoto_download_url;

    const rows = [
      ["Input images", data.input_images],
      ["Images used in reconstruction", data.images_used ?? "-"],
      ["Reconstructed points", data.reconstructed_points ?? "-"],
      ["Average GSD (cm/px)", data.average_gsd_cm ?? "-"],
      ["Covered area (sq m)", data.area_sqm ?? "-"],
      ["Processing time (s)", data.processing_time_s ?? "-"],
      ["Orthophoto file size", data.orthophoto_size_bytes
        ? `${(data.orthophoto_size_bytes / 1024 / 1024).toFixed(1)} MB` : "-"],
    ];
    const table = document.getElementById("stats-table");
    table.innerHTML = rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("");
  }

  function showError(msg) {
    errorWrap.textContent = msg;
    errorWrap.classList.remove("hidden");
  }

  const btnToStep3 = document.getElementById("btn-to-step3");
  if (btnToStep3) btnToStep3.addEventListener("click", () => goToStep(3));

  // ---- Step 3: NGRDI ----------------------------------------------------
  const btnStartNgrdi = document.getElementById("btn-start-ngrdi");
  const ngrdiProgressWrap = document.getElementById("ngrdi-progress-wrap");
  const ngrdiProgressFill = document.getElementById("ngrdi-progress-fill");
  const ngrdiProgressLabel = document.getElementById("ngrdi-progress-label");
  const ngrdiJobLog = document.getElementById("ngrdi-job-log");
  const ngrdiResultWrap = document.getElementById("ngrdi-result-wrap");
  const ngrdiErrorWrap = document.getElementById("ngrdi-error-wrap");
  let ngrdiPollTimer = null;

  if (btnStartNgrdi) {
    btnStartNgrdi.addEventListener("click", async () => {
      if (!projectId) { alert("Run reconstruction first."); return; }
      ngrdiErrorWrap.classList.add("hidden");
      ngrdiResultWrap.classList.add("hidden");
      ngrdiProgressWrap.classList.remove("hidden");
      ngrdiProgressFill.style.width = "1%";
      ngrdiProgressLabel.textContent = "Starting...";
      ngrdiJobLog.textContent = "";
      btnStartNgrdi.disabled = true;

      const opts = {
        vmin: Number(document.getElementById("opt-ngrdi-vmin").value),
        vmax: Number(document.getElementById("opt-ngrdi-vmax").value),
      };

      try {
        const res = await fetch(`/api/ngrdi/${projectId}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(opts),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "failed to start");
        ngrdiPollTimer = setInterval(pollNgrdiStatus, 2000);
      } catch (err) {
        showNgrdiError(err.message);
        btnStartNgrdi.disabled = false;
      }
    });
  }

  async function pollNgrdiStatus() {
    const res = await fetch(`/api/ngrdi/status/${projectId}`);
    const job = await res.json();
    if (!res.ok) return;

    ngrdiProgressFill.style.width = `${job.progress || 0}%`;
    ngrdiProgressLabel.textContent = job.log.length ? job.log[job.log.length - 1] : "Working...";
    ngrdiJobLog.textContent = job.log.join("\n");
    ngrdiJobLog.scrollTop = ngrdiJobLog.scrollHeight;

    if (job.status === "done") {
      clearInterval(ngrdiPollTimer);
      btnStartNgrdi.disabled = false;
      await loadNgrdiResult();
    } else if (job.status === "error") {
      clearInterval(ngrdiPollTimer);
      btnStartNgrdi.disabled = false;
      showNgrdiError(job.error || "NGRDI analysis failed.");
    }
  }

  async function loadNgrdiResult() {
    const res = await fetch(`/api/ngrdi/result/${projectId}`);
    const data = await res.json();
    if (!res.ok) { showNgrdiError(data.error || "Could not load NGRDI result."); return; }

    ngrdiProgressWrap.classList.add("hidden");
    ngrdiResultWrap.classList.remove("hidden");
    const img = document.getElementById("ngrdi-preview");
    img.src = data.preview_url + `?t=${Date.now()}`;

    document.getElementById("ngrdi-raw-download").href = data.raw_download_url;
    document.getElementById("ngrdi-colored-download").href = data.colored_download_url;
    document.getElementById("ngrdi-time-label").textContent = `Processed in ${data.processing_time_s}s`;
  }

  function showNgrdiError(msg) {
    ngrdiErrorWrap.textContent = msg;
    ngrdiErrorWrap.classList.remove("hidden");
  }

  const btnToStep4 = document.getElementById("btn-to-step4");
  if (btnToStep4) btnToStep4.addEventListener("click", () => goToStep(4));

  // ---- Models: list / upload -------------------------------------------
  async function loadModels() {
    const res = await fetch("/api/models");
    const data = await res.json();
    optYoloModel.innerHTML = "";
    if (!data.models || data.models.length === 0) {
      optYoloModel.innerHTML = "<option value=''>No models uploaded yet</option>";
      return;
    }
    data.models.forEach(name => {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      optYoloModel.appendChild(opt);
    });
  }

  if (btnUploadModel) {
    btnUploadModel.addEventListener("click", () => modelFileInput.click());
    modelFileInput.addEventListener("change", async () => {
      const file = modelFileInput.files[0];
      if (!file) return;
      const form = new FormData();
      form.append("model", file);
      btnUploadModel.disabled = true;
      btnUploadModel.textContent = "Uploading...";
      try {
        const res = await fetch("/api/models", { method: "POST", body: form });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "upload failed");
        await loadModels();
      } catch (err) {
        alert(`Model upload failed: ${err.message}`);
      } finally {
        btnUploadModel.disabled = false;
        btnUploadModel.textContent = "+ Upload model (.pt)";
        modelFileInput.value = "";
      }
    });
  }

  // ---- Step 4: YOLOv8 detection ------------------------------------------
  if (btnStartYolo) {
    btnStartYolo.addEventListener("click", async () => {
      if (!projectId) { alert("Run reconstruction first."); return; }
      const model = optYoloModel.value;
      if (!model) { alert("Upload or select a model first."); return; }

      yoloErrorWrap.classList.add("hidden");
      yoloResultWrap.classList.add("hidden");
      yoloProgressWrap.classList.remove("hidden");
      yoloProgressFill.style.width = "1%";
      yoloProgressLabel.textContent = "Starting...";
      yoloJobLog.textContent = "";
      btnStartYolo.disabled = true;

      const opts = {
        model,
        conf: Number(document.getElementById("opt-yolo-conf").value),
        iou: Number(document.getElementById("opt-yolo-iou").value),
        tile_size: Number(document.getElementById("opt-yolo-tile").value),
      };

      try {
        const res = await fetch(`/api/yolo/${projectId}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(opts),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "failed to start");
        yoloPollTimer = setInterval(pollYoloStatus, 2000);
      } catch (err) {
        showYoloError(err.message);
        btnStartYolo.disabled = false;
      }
    });
  }

  async function pollYoloStatus() {
    const res = await fetch(`/api/yolo/status/${projectId}`);
    const job = await res.json();
    if (!res.ok) return;

    yoloProgressFill.style.width = `${job.progress || 0}%`;
    yoloProgressLabel.textContent = job.log.length ? job.log[job.log.length - 1] : "Working...";
    yoloJobLog.textContent = job.log.join("\n");
    yoloJobLog.scrollTop = yoloJobLog.scrollHeight;

    if (job.status === "done") {
      clearInterval(yoloPollTimer);
      btnStartYolo.disabled = false;
      await loadYoloResult();
    } else if (job.status === "error") {
      clearInterval(yoloPollTimer);
      btnStartYolo.disabled = false;
      showYoloError(job.error || "Detection failed.");
    }
  }

  async function loadYoloResult() {
    const res = await fetch(`/api/yolo/result/${projectId}`);
    const data = await res.json();
    if (!res.ok) { showYoloError(data.error || "Could not load detection result."); return; }

    yoloProgressWrap.classList.add("hidden");
    yoloResultWrap.classList.remove("hidden");
    const img = document.getElementById("yolo-preview");
    img.src = data.annotated_url + `?t=${Date.now()}`;
    document.getElementById("yolo-download").href = data.annotated_url;

    const rows = [
      ["Model", data.model_file],
      ["Tiles processed", data.total_tiles ?? "-"],
      ["Total detections", data.total_detections],
      ["Confidence threshold", data.conf],
      ["IOU threshold", data.iou],
      ["Processing time (s)", data.processing_time_s],
    ];
    Object.entries(data.counts_by_class || {}).forEach(([name, count]) => {
      rows.push([`  - ${name}`, count]);
    });
    const table = document.getElementById("yolo-stats-table");
    table.innerHTML = rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("");
  }

  function showYoloError(msg) {
    yoloErrorWrap.textContent = msg;
    yoloErrorWrap.classList.remove("hidden");
  }

  const btnToStep5 = document.getElementById("btn-to-step5");
  if (btnToStep5) btnToStep5.addEventListener("click", () => goToStep(5));

  loadModels();

  // ---- Step 5: full PDF report ------------------------------------------
  const btnGenerateReport = document.getElementById("btn-generate-report");
  const reportProgressLabel = document.getElementById("report-progress-label");
  const reportResultWrap = document.getElementById("report-result-wrap");
  const reportErrorWrap = document.getElementById("report-error-wrap");

  if (btnGenerateReport) {
    btnGenerateReport.addEventListener("click", async () => {
      if (!projectId) { alert("Run reconstruction first."); return; }
      reportErrorWrap.classList.add("hidden");
      reportResultWrap.classList.add("hidden");
      reportProgressLabel.classList.remove("hidden");
      btnGenerateReport.disabled = true;

      try {
        const res = await fetch(`/api/report/generate/${projectId}`, { method: "POST" });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "failed to generate report");
        await loadFullReport();
      } catch (err) {
        showReportError(err.message);
      } finally {
        reportProgressLabel.classList.add("hidden");
        btnGenerateReport.disabled = false;
      }
    });
  }

  // ---- Leaflet Map Logic ----
  let map = null;

  async function initReportMap(projectId) {
    const mapElem = document.getElementById("report-map");
    if (!mapElem) return;

    // Destroy previous instance if re-opening to prevent Leaflet errors
    if (map) {
      map.remove();
      map = null;
    }

    try {
      // 1. Fetch Leaflet geographic bounds from Flask backend
      const boundsRes = await fetch(`/api/project/${projectId}/bounds`);
      if (!boundsRes.ok) return;
      const { bounds } = await boundsRes.json();

      // 2. Initialize Leaflet map
      map = L.map('report-map');
      
      const esriSat = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
        attribution: 'Tiles &copy; Esri'
      }).addTo(map);

      const overlayLayers = {};

      // 3. Add Base Orthophoto Preview
      const orthoUrl = `/files/${projectId}/report/orthomosaic_preview.png`;
      const orthoLayer = L.imageOverlay(orthoUrl, bounds).addTo(map);
      overlayLayers["Orthomosaic"] = orthoLayer;

      // 4. Add NGRDI Layer (if generated)
      try {
        const ngrdiRes = await fetch(`/api/ngrdi/result/${projectId}`);
        if (ngrdiRes.ok) {
          const ngrdiData = await ngrdiRes.json();
          const ngrdiLayer = L.imageOverlay(ngrdiData.preview_url, bounds);
          overlayLayers["NGRDI"] = ngrdiLayer;
        }
      } catch (e) {}

      // 5. Add YOLO Detections Layer (if generated)
      try {
        const yoloRes = await fetch(`/api/yolo/result/${projectId}`);
        if (yoloRes.ok) {
          const yoloData = await yoloRes.json();
          const yoloLayer = L.imageOverlay(yoloData.annotated_url, bounds);
          overlayLayers["YOLO Detections"] = yoloLayer;
        }
      } catch (e) {}

      // 6. Add Layer Toggle Control widget to map
      L.control.layers({ "Satellite Base": esriSat }, overlayLayers, { collapsed: false }).addTo(map);

      // Fit view automatically to bounds of orthophoto
      map.fitBounds(bounds);
    } catch (err) {
      console.error("Map initialization failed:", err);
    }
  }

  async function loadFullReport() {
    const res = await fetch(`/api/report/full/${projectId}`);
    const data = await res.json();
    if (!res.ok) { showReportError(data.error || "Could not load report."); return; }

    reportResultWrap.classList.remove("hidden");

    setTimeout(() => initReportMap(projectId), 100); // Delay to ensure map container is visible

    document.getElementById("report-pdf-download").href = data.pdf_download_url;

    const odm = data.odm || {};
    const ngrdi = data.ngrdi;
    const yolo = data.yolo;

    const rows = [
      ["Input images", data.input_images],
      ["Images used in reconstruction", odm.images_used ?? "-"],
      ["Reconstructed points", odm.points ?? "-"],
      ["Average GSD (cm/px)", odm.gsd_cm ?? "-"],
      ["Area covered (sq m)", odm.area_sqm ?? "-"],
      ["Reconstruction time (s)", odm.processing_time_s ?? "-"],
    ];

    if (ngrdi) {
      rows.push(["NGRDI processing time (s)", ngrdi.processing_time_s ?? "-"]);
      rows.push(["NGRDI vmin / vmax", `${ngrdi.vmin ?? "-"} / ${ngrdi.vmax ?? "-"}`]);
    } else {
      rows.push(["NGRDI", "not run yet"]);
    }

    if (yolo) {
      rows.push(["YOLOv8 model", yolo.model_file ?? "-"]);
      rows.push(["YOLOv8 tiles processed", yolo.total_tiles ?? "-"]);
      rows.push(["YOLOv8 total detections", yolo.total_detections ?? "-"]);
      rows.push(["YOLOv8 processing time (s)", yolo.processing_time_s ?? "-"]);
    } else {
      rows.push(["YOLOv8 detection", "not run yet"]);
    }

    const table = document.getElementById("report-stats-table");
    table.innerHTML = rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("");
  }

  function showReportError(msg) {
    reportErrorWrap.textContent = msg;
    reportErrorWrap.classList.remove("hidden");
  }

  // ---- Tile download logic (Step 2) -------------------------------------
  const btnDownloadTiles = document.getElementById("btn-download-tiles");
  const tileSizeSelect = document.getElementById("tile-size");
  if (btnDownloadTiles) {
    btnDownloadTiles.addEventListener("click", () => {
      if (!projectId) return;
      const size = tileSizeSelect.value;
      // Triggers browser download without needing XHR logic
      window.location.href = `/api/project/${projectId}/tiles?size=${size}`;
    });
  }

  // ---- System stats polling --------------------------------------------
  const sysCpuBar = document.getElementById("sys-cpu-bar");
  const sysRamBar = document.getElementById("sys-ram-bar");
  const sysDiskBar = document.getElementById("sys-disk-bar");

  const sysCpuVal = document.getElementById("sys-cpu-val");
  const sysRamVal = document.getElementById("sys-ram-val");
  const sysDiskVal = document.getElementById("sys-disk-val");

  function setSysBar(bar, valElem, val) {
    if (!bar) return;
    const percent = Math.round(val);
    
    bar.style.width = `${percent}%`;
    if (valElem) valElem.textContent = `${percent}%`;
    
    bar.className = "sys-bar-fill"; // Reset existing color classes
    
    // Apply green-yellow-orange-red scale
    if (val < 60) bar.classList.add("bg-green");
    else if (val < 80) bar.classList.add("bg-yellow");
    else if (val < 90) bar.classList.add("bg-orange");
    else bar.classList.add("bg-red");
  }

  async function fetchSystemStats() {
    try {
      const res = await fetch("/api/system_usage");
      if (res.ok) {
        const data = await res.json();
        setSysBar(sysCpuBar, sysCpuVal, data.cpu);
        setSysBar(sysRamBar, sysRamVal, data.ram);
        setSysBar(sysDiskBar, sysDiskVal, data.disk);
      }
    } catch (err) {
      // Quiet fail if the server disconnects temporarily
    }
  }
  
  // Init instantly, then poll every 5 seconds
  fetchSystemStats();
  setInterval(fetchSystemStats, 5000);

})();