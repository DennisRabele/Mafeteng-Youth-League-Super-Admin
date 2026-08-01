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

  const navDrawer = document.querySelector("[data-nav-drawer]");
  const navBackdrop = document.querySelector("[data-nav-backdrop]");
  const navToggle = document.querySelector("[data-nav-toggle]");
  const navClose = document.querySelector("[data-nav-close]");

  const openNav = () => {
    if (!navDrawer || !navBackdrop) {
      return;
    }
    navToggle?.setAttribute("aria-expanded", "true");
    document.body.classList.add("nav-open");
    navDrawer.hidden = false;
    navBackdrop.hidden = false;
    window.requestAnimationFrame(() => {
      navDrawer.classList.add("is-open");
      navBackdrop.classList.add("is-visible");
      navDrawer.querySelector("button, a, [tabindex]:not([tabindex='-1'])")?.focus?.();
    });
  };

  const closeNav = () => {
    if (!navDrawer || !navBackdrop) {
      return;
    }
    navToggle?.setAttribute("aria-expanded", "false");
    document.body.classList.remove("nav-open");
    navDrawer.classList.remove("is-open");
    navBackdrop.classList.remove("is-visible");
    window.setTimeout(() => {
      navDrawer.hidden = true;
      navBackdrop.hidden = true;
    }, 180);
  };

  if (navToggle) {
    navToggle.addEventListener("click", (event) => {
      event.preventDefault();
      if (navDrawer?.hidden) {
        openNav();
      } else {
        closeNav();
      }
    });
  }
  if (navClose) {
    navClose.addEventListener("click", closeNav);
  }
  if (navBackdrop) {
    navBackdrop.addEventListener("click", closeNav);
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeNav();
    }
  });

  document.querySelectorAll("[data-nav-section-target]").forEach((button) => {
    button.addEventListener("click", () => {
      const target = button.dataset.navSectionTarget;
      const dashboardRoute = button.dataset.dashboardRoute || "/";
      const matchingSectionButton = target
        ? document.querySelector(`[data-section-target="${target}"]`)
        : null;
      closeNav();
      if (matchingSectionButton) {
        matchingSectionButton.click();
        return;
      }
      showLoading();
      const destination = new URL(dashboardRoute, window.location.origin);
      if (target) {
        destination.hash = target;
      }
      window.location.href = `${destination.pathname}${destination.search}${destination.hash}`;
    });
  });

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-loading-trigger]");
    if (!trigger) {
      return;
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
        if (window.syncDashboardContext) {
          window.syncDashboardContext(target);
        }
        hideLoading();
      }, 350);
    });
  });

  const syncDashboardContext = (target) => {
    const teamAdminContext = document.querySelector("[data-team-admin-workspace]");
    if (teamAdminContext) {
      const registrationSections = new Set(["team-form", "player-form"]);
      teamAdminContext.hidden = !registrationSections.has(target);
    }
  };

  const normalizeCategoryValue = (value) => String(value || "").trim().toLowerCase();

  const applyStatusAndCategoryFilters = (sectionId) => {
    const section = document.getElementById(sectionId);
    if (!section) {
      return;
    }
    const statusFilter = section.dataset.statusFilter || "all";
    const categoryFilter = section.dataset.categoryFilter || "all";
    const normalizedCategoryFilter = normalizeCategoryValue(categoryFilter);
    const metricFilter = section.dataset.metricFilter || "all";
    const teamFilter = section.dataset.teamFilter || "all";
    section.querySelectorAll("tbody tr[data-row]").forEach((row) => {
      const rowStatus = row.dataset.status || "all";
      const rowCategory = row.dataset.category || "all";
      const normalizedRowCategory = normalizeCategoryValue(rowCategory);
      const rowMetric = row.dataset.metric || "all";
      const rowTeam = row.dataset.teamId || "all";
      const matchesStatus = statusFilter === "all" || rowStatus === statusFilter;
      const matchesCategory = normalizedCategoryFilter === "all" || normalizedRowCategory === normalizedCategoryFilter;
      const matchesMetric = metricFilter === "all" || rowMetric === metricFilter;
      const matchesTeam = teamFilter === "all" || rowTeam === teamFilter;
      row.dataset.filterHidden = String(!(matchesStatus && matchesCategory && matchesMetric && matchesTeam));
    });
    section.querySelectorAll("[data-filter-chip]").forEach((chip) => {
      chip.classList.toggle("active", chip.dataset.filterValue === statusFilter);
    });
  };

  const syncPaginatedTables = (sectionId) => {
    const section = document.getElementById(sectionId);
    if (!section) {
      return;
    }
    section.querySelectorAll("[data-paginated-table]").forEach((tableRoot) => {
      if (typeof tableRoot.__resetPagination === "function") {
        tableRoot.__resetPagination();
      }
    });
  };

  const filterDashboardRows = (sectionId, status, event) => {
    if (event) {
      event.preventDefault();
    }
    const section = document.getElementById(sectionId);
    if (!section) {
      return;
    }
    section.dataset.statusFilter = status;
    applyStatusAndCategoryFilters(sectionId);
    syncPaginatedTables(sectionId);
  };

  const filterDashboardCategory = (sectionId, category, event) => {
    if (event) {
      event.preventDefault();
    }
    const section = document.getElementById(sectionId);
    if (!section) {
      return;
    }
    section.dataset.categoryFilter = category;
    applyStatusAndCategoryFilters(sectionId);
    syncPaginatedTables(sectionId);
  };

  const filterDashboardMetric = (sectionId, metric, event) => {
    if (event) {
      event.preventDefault();
    }
    const section = document.getElementById(sectionId);
    if (!section) {
      return;
    }
    section.dataset.metricFilter = metric;
    const performanceViews = section.querySelectorAll("[data-performance-view]");
    performanceViews.forEach((view) => {
      const viewMetric = view.dataset.performanceView;
      view.hidden = metric !== "all" && viewMetric !== metric;
    });
    applyStatusAndCategoryFilters(sectionId);
    syncPaginatedTables(sectionId);
  };

  const filterPlayerStatistics = (sectionId, category, teamId, event) => {
    if (event) {
      event.preventDefault();
    }
    const section = document.getElementById(sectionId);
    if (!section) {
      return;
    }
    section.dataset.categoryFilter = category;
    section.dataset.teamFilter = teamId;
    syncPlayerStatisticsTeams(sectionId);
    applyStatusAndCategoryFilters(sectionId);
    syncPaginatedTables(sectionId);
  };

  const syncPlayerStatisticsTeams = (sectionId) => {
    const section = document.getElementById(sectionId);
    if (!section) {
      return;
    }
    const categoryFilter = section.dataset.categoryFilter || "all";
    const normalizedCategoryFilter = normalizeCategoryValue(categoryFilter);
    const teamSelect = section.querySelector("#player-statistics-team");
    if (!teamSelect) {
      return;
    }
    if (!teamSelect.dataset.allOptionsCached) {
      teamSelect.dataset.allOptionsCached = "true";
      teamSelect.__allOptions = Array.from(teamSelect.options).map((option) => option.cloneNode(true));
    }
    const allOptions = teamSelect.__allOptions || Array.from(teamSelect.options).map((option) => option.cloneNode(true));
    const filteredOptions = allOptions.filter((option, index) => {
      if (index === 0) {
        return true;
      }
      const optionCategory = normalizeCategoryValue(option.dataset.category || "");
      return normalizedCategoryFilter === "all" || optionCategory === normalizedCategoryFilter;
    });
    teamSelect.replaceChildren(...filteredOptions.map((option) => option.cloneNode(true)));
    const availableValues = new Set(Array.from(teamSelect.options).map((option) => option.value));
    let fallbackValue = "all";
    for (const option of Array.from(teamSelect.options)) {
      if (option.value !== "all") {
        fallbackValue = option.value;
        break;
      }
    }
    const selectedValue = availableValues.has(teamSelect.value)
      ? teamSelect.value
      : availableValues.has(section.dataset.teamFilter || "")
        ? section.dataset.teamFilter
        : fallbackValue;
    teamSelect.value = selectedValue;
    section.dataset.teamFilter = selectedValue;
  };

  const filterDashboardPanels = (sectionId, category, event) => {
    if (event) {
      event.preventDefault();
    }
    const section = document.getElementById(sectionId);
    if (!section) {
      return;
    }
    section.dataset.categoryFilter = category;
    const normalizedCategory = normalizeCategoryValue(category);
    const panels = section.querySelectorAll("[data-category-panel]");
    panels.forEach((panel) => {
      panel.hidden = normalizedCategory !== "all" && normalizeCategoryValue(panel.dataset.categoryPanel) !== normalizedCategory;
    });
    syncPaginatedTables(sectionId);
  };

  window.filterDashboardRows = filterDashboardRows;
  window.filterDashboardCategory = filterDashboardCategory;
  window.filterDashboardMetric = filterDashboardMetric;
  window.filterPlayerStatistics = filterPlayerStatistics;
  window.syncPlayerStatisticsTeams = syncPlayerStatisticsTeams;
  window.filterDashboardPanels = filterDashboardPanels;
  window.syncDashboardContext = syncDashboardContext;
  window.syncPaginatedTables = syncPaginatedTables;

  document.querySelectorAll("[data-paginated-table]").forEach((tableRoot) => {
    const rows = Array.from(tableRoot.querySelectorAll("tbody tr[data-row]"));
    const pageSize = Number(tableRoot.dataset.pageSize || 5);
    const prev = tableRoot.querySelector("[data-page-prev]");
    const next = tableRoot.querySelector("[data-page-next]");
    const label = tableRoot.querySelector("[data-page-label]");
    let page = 0;

    const getVisibleRows = () => rows.filter((row) => row.dataset.filterHidden !== "true");

    const render = () => {
      const visibleRows = getVisibleRows();
      const pageCount = Math.max(1, Math.ceil(visibleRows.length / pageSize));
      page = Math.min(page, pageCount - 1);
      rows.forEach((row) => {
        row.hidden = row.dataset.filterHidden === "true";
      });
      visibleRows.forEach((row, index) => {
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

    tableRoot.__resetPagination = () => {
      page = 0;
      render();
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

  const activateSectionFromLocation = () => {
    const hash = window.location.hash.replace("#", "");
    const search = new URLSearchParams(window.location.search);
    const sectionTarget = hash || search.get("dashboard_section") || "";
    if (!sectionTarget) {
      return;
    }
    const matchingSectionButton = document.querySelector(`[data-section-target="${sectionTarget}"]`);
    if (matchingSectionButton) {
      matchingSectionButton.click();
    }
  };

  const applyInitialCategoryPanelFilters = () => {
    document.querySelectorAll("[data-dashboard-category-filter]").forEach((select) => {
      const section = select.closest("[data-dashboard-section]");
      if (section && section.id && window.filterDashboardPanels) {
        window.filterDashboardPanels(section.id, select.value, null);
      }
    });
  };

  window.addEventListener("pageshow", hideLoading);
  window.addEventListener("hashchange", activateSectionFromLocation);
  activateSectionFromLocation();
  applyInitialCategoryPanelFilters();
  document.querySelectorAll("[data-dashboard-section='player-statistics']").forEach((section) => {
    if (window.syncPlayerStatisticsTeams) {
      window.syncPlayerStatisticsTeams(section.id);
    }
  });
  const activeSectionButton = document.querySelector("[data-section-target].active");
  if (activeSectionButton && window.syncDashboardContext) {
    window.syncDashboardContext(activeSectionButton.dataset.sectionTarget);
  }
})();
