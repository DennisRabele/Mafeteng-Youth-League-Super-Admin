(function () {
  const overlay = document.querySelector("[data-loading-overlay]");
  const showLoading = () => {
    if (overlay) {
      overlay.classList.add("is-active");
    }
  };
  const hideLoading = () => {
    if (overlay) {
      overlay.classList.remove("is-active");
    }
  };

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-loading-trigger], a[href]");
    if (!trigger) {
      return;
    }
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    if (trigger.matches("a[href]")) {
      const href = trigger.getAttribute("href") || "";
      const target = trigger.getAttribute("target");
      if (href.startsWith("#") || target === "_blank" || trigger.hasAttribute("download")) {
        return;
      }
    }
    showLoading();
  });

  document.addEventListener("submit", (event) => {
    if (event.target.matches("form")) {
      showLoading();
    }
  });

  document.querySelectorAll("[data-section-target]").forEach((button) => {
    button.addEventListener("click", () => {
      const target = button.dataset.sectionTarget;
      showLoading();
      window.setTimeout(() => {
        document.querySelectorAll("[data-dashboard-section]").forEach((section) => {
          section.hidden = section.dataset.dashboardSection !== target;
        });
        document.querySelectorAll("[data-section-target]").forEach((item) => {
          item.classList.toggle("active", item.dataset.sectionTarget === target);
        });
        hideLoading();
      }, 350);
    });
  });

  document.querySelectorAll("[data-paginated-table]").forEach((tableRoot) => {
    const rows = Array.from(tableRoot.querySelectorAll("tbody tr[data-row]"));
    const pageSize = Number(tableRoot.dataset.pageSize || 5);
    const prev = tableRoot.querySelector("[data-page-prev]");
    const next = tableRoot.querySelector("[data-page-next]");
    const label = tableRoot.querySelector("[data-page-label]");
    let page = 0;

    const render = () => {
      const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
      rows.forEach((row, index) => {
        row.hidden = index < page * pageSize || index >= (page + 1) * pageSize;
      });
      if (label) {
        label.textContent = `Page ${page + 1} of ${pageCount}`;
      }
      if (prev) {
        prev.disabled = page === 0;
      }
      if (next) {
        next.disabled = page >= pageCount - 1;
      }
    };

    if (prev) {
      prev.addEventListener("click", () => {
        showLoading();
        window.setTimeout(() => {
          page = Math.max(0, page - 1);
          render();
          hideLoading();
        }, 180);
      });
    }
    if (next) {
      next.addEventListener("click", () => {
        showLoading();
        window.setTimeout(() => {
          const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
          page = Math.min(pageCount - 1, page + 1);
          render();
          hideLoading();
        }, 180);
      });
    }

    render();
  });

  window.addEventListener("pageshow", hideLoading);
})();
