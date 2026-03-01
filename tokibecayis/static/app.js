(function () {
  const onlyDigits = (el) => {
    if (!el) return;
    el.addEventListener("input", () => {
      el.value = (el.value || "").replace(/[^\d]/g, "");
    });
  };

  function uniqueSorted(arr) {
    return Array.from(new Set(arr)).sort((a,b)=> String(a).localeCompare(String(b), "tr"));
  }

  async function loadAdres() {
    const res = await fetch("/api/adres");
    return await res.json();
  }

  // ---------- username check (register page) ----------
  async function usernameCheck() {
    const input = document.getElementById("username");
    const status = document.getElementById("usernameStatus");
    const btn = document.getElementById("registerBtn");
    if (!input || !status || !btn) return;

    let t = null;
    input.addEventListener("input", () => {
      if (t) clearTimeout(t);
      const val = (input.value || "").trim();
      status.textContent = "";
      btn.disabled = true;

      t = setTimeout(async () => {
        if (val.length < 3) {
          status.textContent = "En az 3 karakter.";
          btn.disabled = true;
          return;
        }
        const r = await fetch(`/api/username_check?username=${encodeURIComponent(val)}`);
        const j = await r.json();
        status.textContent = j.ok ? "✅ Kullanılabilir" : `❌ Uygun değil: ${j.reason}`;
        btn.disabled = !j.ok;
      }, 250);
    });
  }

  // ---------- MultiSelect dropdown-chip ----------
  function createMultiSelect(container, options, selectedValues) {
    const name = container.dataset.name;
    const state = {
      selected: new Set(selectedValues && selectedValues.length ? selectedValues : ["any"]),
      options
    };

    container.innerHTML = `
      <div class="ms-btn" role="button">
        <div class="ms-chips"></div>
        <div class="ms-caret">▾</div>
      </div>
      <div class="ms-panel"></div>
      <div class="ms-hidden"></div>
    `;

    const btn = container.querySelector(".ms-btn");
    const chips = container.querySelector(".ms-chips");
    const panel = container.querySelector(".ms-panel");
    const hidden = container.querySelector(".ms-hidden");

    function renderHidden() {
      hidden.innerHTML = "";
      for (const v of state.selected) {
        const inp = document.createElement("input");
        inp.type = "hidden";
        inp.name = name;
        inp.value = v;
        hidden.appendChild(inp);
      }
    }

    function renderChips() {
      chips.innerHTML = "";
      const values = Array.from(state.selected);
      if (values.includes("any")) {
        const c = document.createElement("span");
        c.className = "ms-chip";
        c.textContent = "Farketmez";
        chips.appendChild(c);
        return;
      }
      values.forEach(v => {
        const opt = state.options.find(o => o.value === v);
        const c = document.createElement("span");
        c.className = "ms-chip";
        c.textContent = opt ? opt.label : v;
        chips.appendChild(c);
      });
    }

    function renderPanel() {
      panel.innerHTML = "";
      state.options.forEach(o => {
        const row = document.createElement("label");
        row.className = "ms-item";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = state.selected.has(o.value);
        cb.addEventListener("change", () => {
          if (o.value === "any") {
            state.selected = new Set(["any"]);
          } else {
            state.selected.delete("any");
            if (cb.checked) state.selected.add(o.value);
            else state.selected.delete(o.value);
            if (state.selected.size === 0) state.selected.add("any");
          }
          renderPanel();
          renderChips();
          renderHidden();
          container.dispatchEvent(new Event("mschange"));
        });

        const txt = document.createElement("span");
        txt.textContent = o.label;
        row.appendChild(cb);
        row.appendChild(txt);
        panel.appendChild(row);
      });
    }

    function openClose(toggle) {
      if (toggle === undefined) container.classList.toggle("open");
      else container.classList.toggle("open", toggle);
    }

    btn.addEventListener("click", () => openClose());
    document.addEventListener("click", (e) => {
      if (!container.contains(e.target)) openClose(false);
    });

    renderPanel();
    renderChips();
    renderHidden();

    return {
      setOptions(newOptions) {
        state.options = newOptions;
        const valid = new Set(newOptions.map(o => o.value));
        const next = new Set();
        for (const v of state.selected) {
          if (v === "any" || valid.has(v)) next.add(v);
        }
        if (next.size === 0) next.add("any");
        state.selected = next;
        renderPanel(); renderChips(); renderHidden();
      },
      setSelected(values) {
        state.selected = new Set(values && values.length ? values : ["any"]);
        if (state.selected.has("any") && state.selected.size > 1) state.selected = new Set(["any"]);
        renderPanel(); renderChips(); renderHidden();
        container.dispatchEvent(new Event("mschange"));
      },
      getSelected() { return Array.from(state.selected); }
    };
  }

  // ---------- Listing form binding ----------
  function fillSelect(selectEl, options, includeAny=false) {
    if (!selectEl) return;
    const current = selectEl.value;
    selectEl.innerHTML = "";
    if (includeAny) {
      const any = document.createElement("option");
      any.value = "any";
      any.textContent = "Farketmez";
      selectEl.appendChild(any);
    } else {
      const ph = document.createElement("option");
      ph.value = "";
      ph.textContent = "Seçiniz";
      selectEl.appendChild(ph);
    }
    options.forEach(v => {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      selectEl.appendChild(opt);
    });
    if (current) selectEl.value = current;
  }

  function bindMevcut(adresData) {
    const il = document.getElementById("mevcut_il");
    const ilce = document.getElementById("mevcut_ilce");
    const mah = document.getElementById("mevcut_mahalle");
    if (!il || !ilce || !mah) return;

    const iller = uniqueSorted(adresData.map(x => x.cityName));
    fillSelect(il, iller, false);

    il.addEventListener("change", () => {
      const selectedIl = il.value;
      const ilceler = uniqueSorted(adresData.filter(x => x.cityName === selectedIl).map(x => x.districtName));
      fillSelect(ilce, ilceler, false);
      fillSelect(mah, [], false);
    });

    ilce.addEventListener("change", () => {
      const selectedIl = il.value;
      const selectedIlce = ilce.value;
      const mahalleler = uniqueSorted(
        adresData.filter(x => x.cityName === selectedIl && x.districtName === selectedIlce).map(x => x.neighborhoodName)
      );
      fillSelect(mah, mahalleler, false);
    });
  }

  function bindHedef(adresData) {
    const il = document.getElementById("hedef_il");
    if (!il) return;

    const iller = uniqueSorted(adresData.map(x => x.cityName));
    iller.forEach(v => {
      const opt = document.createElement("option");
      opt.value = v; opt.textContent = v;
      il.appendChild(opt);
    });

    const msIlceEl = document.getElementById("ms_hedef_ilce");
    const msMahEl = document.getElementById("ms_hedef_mahalle");
    const msKatEl = document.getElementById("ms_hedef_kat");
    if (!msIlceEl || !msMahEl || !msKatEl) return;

    const msIlce = createMultiSelect(msIlceEl, [{value:"any",label:"Farketmez"}], ["any"]);
    const msMah = createMultiSelect(msMahEl, [{value:"any",label:"Farketmez"}], ["any"]);

    const katVals = ["any","-3","-2","-1","0","1","2","3","4","5","6","7"];
    const msKat = createMultiSelect(msKatEl, katVals.map(v => ({value:v, label:(v==="any"?"Farketmez":v)})), ["any"]);

    function refreshMahalle() {
      const selectedIl = il.value;
      const selectedIlceler = msIlce.getSelected();
      if (!selectedIl || selectedIl === "any" || selectedIlceler.includes("any")) {
        msMah.setOptions([{value:"any",label:"Farketmez"}]);
        msMah.setSelected(["any"]);
        return;
      }
      const mahalleler = uniqueSorted(
        adresData
          .filter(x => x.cityName === selectedIl && selectedIlceler.includes(x.districtName))
          .map(x => x.neighborhoodName)
      );
      msMah.setOptions([{value:"any",label:"Farketmez"}, ...mahalleler.map(m => ({value:m,label:m}))]);
      msMah.setSelected(["any"]);
    }

    il.addEventListener("change", () => {
      const selectedIl = il.value;
      if (!selectedIl || selectedIl === "any") {
        msIlce.setOptions([{value:"any",label:"Farketmez"}]);
        msIlce.setSelected(["any"]);
        msMah.setOptions([{value:"any",label:"Farketmez"}]);
        msMah.setSelected(["any"]);
        return;
      }
      const ilceler = uniqueSorted(adresData.filter(x => x.cityName === selectedIl).map(x => x.districtName));
      msIlce.setOptions([{value:"any",label:"Farketmez"}, ...ilceler.map(d => ({value:d,label:d}))]);
      msIlce.setSelected(["any"]);
      msMah.setOptions([{value:"any",label:"Farketmez"}]);
      msMah.setSelected(["any"]);
    });

    msIlceEl.addEventListener("mschange", refreshMahalle);

    // EDIT fill
    if (window.__EDIT__ && window.__LISTING__) {
      const L = window.__LISTING__;

      // mevcut set later below in main()
      document.getElementById("ucret").value = L.ucret || "any";
      document.getElementById("hedef_oda").value = L.hedef_oda || "any";
      document.getElementById("hedef_bolge").value = L.hedef_bolge || "";
      document.getElementById("hedef_etap").value = L.hedef_etap || "";
      document.getElementById("hedef_not").value = L.hedef_not || "";

      il.value = L.hedef_il || "any";
      il.dispatchEvent(new Event("change"));

      setTimeout(() => {
        msIlce.setSelected(L.hedef_ilce || ["any"]);
        setTimeout(() => {
          refreshMahalle();
          setTimeout(() => {
            msMah.setSelected(L.hedef_mahalle || ["any"]);
          }, 80);
        }, 80);
      }, 80);

      msKat.setSelected(L.hedef_kat || ["any"]);
    }
  }

  async function main() {
    usernameCheck();

    // numeric-only inputs (both sides)
    onlyDigits(document.getElementById("mevcut_bolge"));
    onlyDigits(document.getElementById("mevcut_etap"));
    onlyDigits(document.getElementById("hedef_bolge"));
    onlyDigits(document.getElementById("hedef_etap"));

    // Only on listing form
    const mevcutIl = document.getElementById("mevcut_il");
    if (!mevcutIl) return;

    const adresData = await loadAdres();
    bindMevcut(adresData);
    bindHedef(adresData);

    // EDIT fill mevcut after dropdowns exist
    if (window.__EDIT__ && window.__LISTING__) {
      const L = window.__LISTING__;
      document.getElementById("mevcut_bolge").value = L.mevcut_bolge || "";
      document.getElementById("mevcut_etap").value = L.mevcut_etap || "";
      document.getElementById("mevcut_kat").value = L.mevcut_kat || "";
      document.getElementById("mevcut_oda").value = L.mevcut_oda || "";
      document.getElementById("mevcut_not").value = L.mevcut_not || "";

      document.getElementById("mevcut_il").value = L.mevcut_il;
      document.getElementById("mevcut_il").dispatchEvent(new Event("change"));
      setTimeout(() => {
        document.getElementById("mevcut_ilce").value = L.mevcut_ilce;
        document.getElementById("mevcut_ilce").dispatchEvent(new Event("change"));
        setTimeout(() => {
          document.getElementById("mevcut_mahalle").value = L.mevcut_mahalle;
        }, 80);
      }, 80);
    }
  }

  document.addEventListener("DOMContentLoaded", main);
})();