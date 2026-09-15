"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import Icon from "../ui/Icon";
import { Project, User, api } from "@/lib/api";
import { useWorkspace } from "@/lib/useWorkspace";

interface NavItem {
  label: string;
  icon: string;
  href: string;
  colorClass: string;
  bgClass: string;
  hasArrow?: boolean;
  /** Expands in place to list the workspace's projects. */
  expandable?: boolean;
}

// Grouped so the sidebar reads as "where I work" → "what it adds up to" →
// "who and how it is set up", instead of one flat list of ten links.
const groups: { title: string; items: NavItem[] }[] = [
  {
    title: "Làm việc",
    items: [
      {
        label: "Tổng quan",
        icon: "dashboard",
        href: "/",
        colorClass: "text-blue-600",
        bgClass: "bg-blue-50 text-blue-600",
        hasArrow: true,
      },
      {
        label: "Dự án",
        icon: "folder_open",
        href: "/projects",
        colorClass: "text-amber-600",
        bgClass: "bg-amber-50 text-amber-600",
        hasArrow: true,
        expandable: true,
      },
      {
        label: "Lịch",
        icon: "calendar_today",
        href: "/calendar",
        colorClass: "text-emerald-600",
        bgClass: "bg-emerald-50 text-emerald-600",
        hasArrow: true,
      },
      {
        label: "Chấm công",
        icon: "schedule",
        href: "/timesheet",
        colorClass: "text-rose-600",
        bgClass: "bg-rose-50 text-rose-600",
        hasArrow: true,
      },
    ],
  },
  {
    title: "Phân tích",
    items: [
      {
        label: "Báo cáo",
        icon: "monitoring",
        href: "/reports",
        colorClass: "text-purple-600",
        bgClass: "bg-purple-50 text-purple-600",
        hasArrow: true,
      },
    ],
  },
  {
    title: "Tổ chức",
    items: [
      {
        label: "Nhân sự",
        icon: "group",
        href: "/team",
        colorClass: "text-sky-600",
        bgClass: "bg-sky-50 text-sky-600",
        hasArrow: true,
      },
    ],
  },
];

// Theo dõi xem có đang ở breakpoint desktop (lg, >=1024px) hay không.
// Cần biết chính xác để quyết định "compact" nên đọc theo collapsed (desktop)
// hay theo mobileOpen (mobile/tablet).
function useIsDesktop() {
  const [isDesktop, setIsDesktop] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 1024px)");
    setIsDesktop(mq.matches);
    const handler = (e: MediaQueryListEvent) => setIsDesktop(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);
  return isDesktop;
}

export default function Sidebar({
  user,
  collapsed = false,
  onToggle,
  mobileOpen = false,
  onMobileToggle,
}: {
  user: User | null;
  collapsed?: boolean;
  onToggle?: () => void;
  mobileOpen?: boolean;
  onMobileToggle?: () => void;
}) {
  const pathname = usePathname();
  const { workspaceId } = useWorkspace();
  const [projects, setProjects] = useState<Project[]>([]);
  // Start expanded when you're already inside a project, so the sidebar shows
  // where you are rather than making you expand it again every navigation.
  const [expanded, setExpanded] = useState(() => pathname.startsWith("/projects/"));
  const isDesktop = useIsDesktop();

  // Desktop: compact = collapsed (do người dùng tự thu gọn, nhớ trong localStorage).
  // Mobile/Tablet: mặc định compact (thanh 70px), phình ra full khi mobileOpen = true.
  const compact = isDesktop ? collapsed : !mobileOpen;

  // Bấm nút thu gọn/mở rộng: desktop dùng onToggle, mobile/tablet dùng onMobileToggle.
  const handleToggleClick = () => (isDesktop ? onToggle?.() : onMobileToggle?.());

  // Sau khi chọn 1 mục điều hướng trên mobile, tự thu lại về 70px.
  const closeOnMobile = () => {
    if (!isDesktop && mobileOpen) onMobileToggle?.();
  };

  useEffect(() => {
    if (!workspaceId) return;
    api.listProjects(workspaceId).then(setProjects).catch(() => setProjects([]));
  }, [workspaceId]);

  const isActive = (href: string) =>
    href === "/" ? pathname === "/" : pathname.startsWith(href);

  return (
    <>
      {/* Overlay chỉ xuất hiện khi đang mở rộng trên mobile/tablet */}
      {mobileOpen && (
        <div className="fixed inset-0 bg-black/40 z-40 lg:hidden" onClick={onMobileToggle} />
      )}

      {/* Nút nổi mở sidebar — chỉ hiện dưới 640px (mobile nhỏ), khi sidebar
          đang ẩn hoàn toàn ngoài màn hình nên nút toggle bên trong aside
          không thể bấm được. */}
      {!mobileOpen && (
        <button
          onClick={onMobileToggle}
          className="sm:hidden fixed top-3 left-3 z-50 w-10 h-10 rounded-full bg-white border border-outline-variant/30 shadow-md flex items-center justify-center text-gray-600 hover:bg-gray-50"
          aria-label="Mở thanh điều hướng"
        >
          <Icon name="menu" size={20} />
        </button>
      )}

      <aside
        className={`fixed left-0 top-0 h-full flex flex-col py-md bg-white border-r border-outline-variant/30 z-50 overflow-y-auto overflow-x-hidden transition-all duration-300
          w-[260px]
          ${mobileOpen ? "translate-x-0" : "-translate-x-full"} sm:translate-x-0
          ${mobileOpen ? "sm:w-[260px]" : "sm:w-[70px]"}
          ${collapsed ? "lg:w-[70px]" : "lg:w-[260px]"}
        `}
      >
        {/* Top Header */}
        {compact ? (
          <div className="flex flex-col items-center gap-2 mb-6 px-2">
            <img
              src="/logo.svg"
              alt="Flowie Logo"
              className="w-8 h-8 rounded-lg cursor-pointer shadow-sm"
              onClick={handleToggleClick}
              title="Mở rộng thanh bên"
            />
            <button
              onClick={handleToggleClick}
              className="p-1.5 hover:text-gray-700 hover:bg-gray-100 rounded-md transition-colors text-gray-400"
              title="Mở rộng thanh bên"
              aria-label="Mở rộng thanh bên"
            >
              <Icon name="vertical_split" size={18} />
            </button>
          </div>
        ) : (
          <div className="flex items-center justify-between px-lg mb-lg">
            <div className="flex items-center gap-sm min-w-0">
              <img src="/logo.svg" alt="Flowie Logo" className="w-8 h-8 rounded-lg shadow-sm shrink-0" />
              <span className="font-bold text-[16px] text-gray-900 tracking-tight truncate">Flowie</span>
            </div>
            <div className="flex items-center gap-1 text-gray-400">
              <button
                className="p-1 hover:text-gray-700 hover:bg-gray-100 rounded-md transition-colors"
                title="Quick Actions"
              >
                <Icon name="bolt" size={18} />
              </button>
              <button
                onClick={handleToggleClick}
                className="p-1 hover:text-gray-700 hover:bg-gray-100 rounded-md transition-colors text-gray-500"
                title="Thu gọn thanh bên"
                aria-label="Thu gọn thanh bên"
              >
                <Icon name="vertical_split" size={18} />
              </button>
            </div>
          </div>
        )}

        {/* Grouped navigation */}
        <nav className="flex flex-col gap-lg mb-lg">
          {groups.map((group) => (
            <div key={group.title}>
              {!compact && (
                <p className="px-md mb-1 text-[11px] font-bold uppercase tracking-wider text-gray-400">
                  {group.title}
                </p>
              )}
              <div className={`flex flex-col gap-[2px] ${compact ? "px-1" : "px-sm"}`}>
                {group.items.map((item) => {
                  const active = isActive(item.href);
                  const canExpand = item.expandable && projects.length > 0;
                  return (
                    <div key={item.href}>
                      <div
                        className={`group flex items-center rounded-md transition-all ${
                          active
                            ? "bg-gray-100/90 text-gray-900 font-semibold"
                            : "text-gray-600 hover:bg-gray-50"
                        }`}
                      >
                        <Link
                          href={item.href}
                          onClick={closeOnMobile}
                          title={compact ? item.label : undefined}
                          className={`flex items-center gap-3 flex-grow min-w-0 ${
                            compact ? "justify-center px-0 py-2" : "px-3 py-2"
                          }`}
                        >
                          <div
                            className={`rounded-lg flex items-center justify-center shrink-0 transition-transform group-hover:scale-105 ${
                              compact ? "w-8 h-8" : "w-7 h-7"
                            } ${item.bgClass}`}
                          >
                            <Icon name={item.icon} size={17} />
                          </div>
                          {!compact && <span className="text-[14px] truncate">{item.label}</span>}
                        </Link>
                        {/* The chevron used to be decorative — part of the link.
                            For "Dự án" it now toggles the project list in place. */}
                        {!compact &&
                          (canExpand ? (
                            <button
                              onClick={() => setExpanded((v) => !v)}
                              aria-label={expanded ? "Thu gọn danh sách dự án" : "Mở danh sách dự án"}
                              aria-expanded={expanded}
                              className="px-2 py-2 text-gray-300 hover:text-gray-700 transition-colors"
                            >
                              <Icon name={expanded ? "expand_more" : "chevron_right"} size={18} />
                            </button>
                          ) : item.hasArrow ? (
                            <Link href={item.href} onClick={closeOnMobile} className="px-2 py-2">
                              <Icon
                                name="chevron_right"
                                size={18}
                                className="text-gray-300 group-hover:text-gray-500 transition-colors"
                              />
                            </Link>
                          ) : null)}
                      </div>

                      {!compact && item.expandable && expanded && (
                        <div className="ml-[26px] mt-[2px] flex flex-col gap-[2px] border-l border-gray-100 pl-2">
                          {projects.map((p) => {
                            const on = pathname.startsWith(`/projects/${p.id}`);
                            return (
                              <Link
                                key={p.id}
                                href={`/projects/${p.id}`}
                                onClick={closeOnMobile}
                                title={p.name}
                                className={`flex items-center gap-2 px-2 py-1.5 rounded-md text-[13px] transition-colors ${
                                  on
                                    ? "bg-gray-100 text-gray-900 font-semibold"
                                    : "text-gray-500 hover:bg-gray-50"
                                }`}
                              >
                                <span className="chip bg-primary-container/10 text-primary shrink-0">{p.key}</span>
                                <span className="truncate">{p.name}</span>
                              </Link>
                            );
                          })}
                          {projects.length === 0 && (
                            <span className="px-2 py-1.5 text-[13px] text-gray-400">Chưa có dự án</span>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </nav>

        <div className={`mt-auto pt-4 flex flex-col gap-1 ${compact ? "px-1" : "px-md"}`}>
          {user?.isSystemAdmin && (
            <Link
              href="/admin"
              onClick={closeOnMobile}
              title={compact ? "Admin Panel" : undefined}
              className={`flex items-center ${
                compact ? "justify-center px-0 py-2" : "justify-between px-3 py-2"
              } rounded-xl transition-all ${
                isActive("/admin")
                  ? "bg-gray-100/90 text-gray-900 font-bold"
                  : "text-gray-600 hover:bg-gray-50"
              }`}
            >
              <div className="flex items-center gap-3 min-w-0">
                <div
                  className={`rounded-lg flex items-center justify-center shrink-0 bg-violet-50 text-violet-600 ${
                    compact ? "w-8 h-8" : "w-7 h-7"
                  }`}
                >
                  <Icon name="admin_panel_settings" size={17} />
                </div>
                {!compact && <span className="text-[14px]">Admin Panel</span>}
              </div>
            </Link>
          )}
          <Link
            href="/settings"
            onClick={closeOnMobile}
            title={compact ? "Settings" : undefined}
            className={`flex items-center ${
              compact ? "justify-center px-0 py-2" : "justify-between px-3 py-2"
            } rounded-xl transition-all ${
              isActive("/settings")
                ? "bg-gray-100/90 text-gray-900 font-medium"
                : "text-gray-600 hover:bg-gray-50"
            }`}
          >
            <div className="flex items-center gap-3 min-w-0">
              <div
                className={`rounded-lg flex items-center justify-center shrink-0 bg-slate-100 text-slate-600 ${
                  compact ? "w-8 h-8" : "w-7 h-7"
                }`}
              >
                <Icon name="settings" size={17} />
              </div>
              {!compact && <span className="text-[14px]">Settings</span>}
            </div>
          </Link>
        </div>

        {/* User Profile Footer */}
        {user && (
          <div
            className={`mt-2 mb-2 p-2 flex items-center ${
              compact ? "justify-center mx-1" : "justify-between mx-sm p-3"
            } rounded-xl hover:bg-gray-50 cursor-pointer border border-gray-100 relative group`}
            onClick={async () => {
              await api.logout();
              window.location.href = "/login";
            }}
            title={compact ? `Đăng xuất (${user.displayName || user.email})` : undefined}
          >
            <div className="flex items-center gap-3 min-w-0">
              {user.avatarUrl ? (
                <img src={user.avatarUrl} alt="User" className="w-9 h-9 rounded-full object-cover shrink-0 border border-gray-200" />
              ) : (
                <div className="w-9 h-9 rounded-full bg-blue-100 text-blue-600 flex items-center justify-center font-bold shrink-0">
                  {user.displayName?.charAt(0).toUpperCase() || user.email.charAt(0).toUpperCase()}
                </div>
              )}
              {!compact && (
                <div className="flex flex-col min-w-0">
                  <span className="text-[14px] font-semibold text-gray-900 truncate">{user.displayName || "Thành viên"}</span>
                  <span className="text-[11px] text-gray-400 truncate">{user.email}</span>
                </div>
              )}
            </div>
            {!compact && (
              <Icon name="logout" size={20} className="text-gray-400 opacity-0 group-hover:opacity-100 transition-opacity" />
            )}
          </div>
        )}
      </aside>
    </>
  );
}