"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, User, Workspace } from "@/lib/api";
import Icon from "../ui/Icon";
import NotificationBell from "./NotificationBell";
import TimerWidget from "../task/TimerWidget";
import ThemeToggle from "./ThemeToggle";

export default function TopBar({
  title,
  user,
  actions,
}: {
  title: React.ReactNode;
  user: User | null;
  actions?: React.ReactNode;
}) {
  const [menu, setMenu] = useState(false);
  const [wsMenu, setWsMenu] = useState(false);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [activeWorkspaceId, setActiveWorkspaceId] = useState<string | null>(null);

  useEffect(() => {
    setActiveWorkspaceId(localStorage.getItem('activeWorkspaceId'));
    if (user) {
      api.listWorkspaces().then(res => setWorkspaces(res || [])).catch(() => {});
    }
  }, [user]);

  return (
    <header className="flex flex-col w-full sticky top-0 z-40 bg-white">
      <div className="flex justify-between items-center px-3 md:px-lg h-14 md:h-16 border-b border-outline-variant/30 gap-2 md:gap-6">
        {/* Left: Search Bar */}
        <div className="flex items-center gap-2 md:gap-6 flex-1 min-w-0">
          <div className="relative flex items-center hidden sm:flex flex-1 max-w-sm">
            <Icon
              name="search"
              size={18}
              className="absolute left-3 text-gray-400"
            />
            <input
              className="bg-gray-50/80 border border-gray-100 rounded-full pl-9 pr-12 py-2 w-full text-xs md:text-[13px] outline-none placeholder-gray-400 focus:bg-white focus:border-gray-200 transition-all shadow-sm"
              placeholder="Search ..."
              type="text"
            />
            <div className="absolute right-3 flex items-center">
              <span className="bg-white border border-gray-200 text-gray-500 rounded px-1 py-0.5 text-[9px] md:text-[10px] font-medium shadow-sm">
                ⌘K
              </span>
            </div>
          </div>
          
          {/* Workspace Selector */}
          <div className="relative hidden md:block">
            <button 
              onClick={() => setWsMenu(!wsMenu)}
              className="flex items-center gap-2 px-2 md:px-3 py-1 md:py-1.5 bg-gray-50 border border-gray-200 rounded-lg text-xs md:text-[13px] font-semibold text-gray-700 hover:bg-gray-100 transition-colors whitespace-nowrap"
            >
              <Icon name="workspaces" size={14} className="text-gray-700 md:size-4" />
              <span className="max-w-[100px] md:max-w-[150px] truncate">
                {workspaces.find(w => w.id === activeWorkspaceId)?.name || "Select Workspace"}
              </span>
              <Icon name="unfold_more" size={14} className="text-gray-400 md:size-4" />
            </button>
            {wsMenu && (
              <div className="absolute left-0 mt-2 w-60 md:w-64 bg-white border border-gray-100 rounded-xl shadow-lg p-2 z-50">
                <div className="px-3 py-2 text-[11px] font-bold text-gray-400 uppercase tracking-wider">
                  Your Workspaces
                </div>
                {workspaces.length === 0 ? (
                  <div className="px-3 py-4 text-center text-[13px] text-gray-500">No workspaces found</div>
                ) : (
                  <div className="flex flex-col gap-1 max-h-60 overflow-y-auto">
                    {workspaces.map(ws => (
                      <Link 
                        key={ws.id} 
                        href={`/workspaces/${ws.id}`}
                        onClick={() => {
                          setWsMenu(false);
                          localStorage.setItem('activeWorkspaceId', ws.id);
                          setActiveWorkspaceId(ws.id);
                        }}
                        className="flex items-center gap-3 px-3 py-2 rounded-lg hover:bg-gray-50 group transition-colors"
                      >
                        <div className="w-8 h-8 rounded-lg bg-gray-100 group-hover:bg-white flex items-center justify-center text-gray-500 group-hover:text-gray-900 shrink-0">
                           <Icon name="folder" size={16} />
                        </div>
                        <div className="min-w-0 flex-1">
                          <p className="text-[13px] font-semibold text-gray-900 truncate">{ws.name}</p>
                          <p className="text-[11px] text-gray-500 truncate">/{ws.slug}</p>
                        </div>
                      </Link>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>

        {/* Center: Empty Space for clean look */}
        <div className="flex-1 hidden md:block"></div>

        {/* Right: Actions & User */}
        <div className="flex items-center gap-2 md:gap-4 shrink-0">
          <div className="hidden md:block">
            <TimerWidget />
          </div>
          <div className="flex items-center gap-2 md:gap-3 text-gray-400">
            <NotificationBell />
            <div className="hidden sm:block">
              <ThemeToggle />
            </div>
            <button className="hidden md:block hover:text-gray-600 transition-colors">
              <Icon name="help_outline" size={22} />
            </button>
            <button className="hidden md:block hover:text-gray-600 transition-colors">
              <Icon name="settings" size={22} />
            </button>
          </div>

          {user && (
            <div className="relative">
              <button
                onClick={() => setMenu((m) => !m)}
                className="w-8 md:w-9 h-8 md:h-9 rounded-full overflow-hidden border border-gray-200 focus:ring-2 focus:ring-gray-200 transition-all bg-gray-50 flex items-center justify-center shrink-0"
              >
                <img 
                  src={user.avatarUrl || `https://ui-avatars.com/api/?name=${encodeURIComponent(user.displayName || user.email)}&background=random`} 
                  alt="Avatar" 
                  className="w-full h-full object-cover" 
                />
              </button>
              {menu && (
                <div className="absolute right-0 mt-2 w-52 md:w-56 bg-white border border-gray-100 rounded-xl shadow-lg p-2 z-50">
                  <div className="px-3 py-2">
                    <p className="text-xs md:text-[14px] font-semibold text-gray-900 truncate">{user.displayName || "Thành viên"}</p>
                    <p className="text-[11px] md:text-[12px] text-gray-500 truncate">
                      {user.email}
                    </p>
                  </div>
                  <div className="h-px bg-gray-100 my-1" />
                  <button
                    onClick={async () => {
                      await api.logout();
                      window.location.href = "/login";
                    }}
                    className="w-full text-left px-3 py-2 rounded-lg hover:bg-gray-50 text-xs md:text-[13px] font-medium text-gray-700 flex items-center gap-2"
                  >
                    <Icon name="logout" size={18} />
                    Đăng xuất
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Page header row.
          `title` and `actions` were accepted as props but never rendered, so
          every page-level button in the app (Dự án mới, Sprint mới, Tạo mới,
          xuất CSV…) was invisible. */}
      {(title || actions) && (
        <div className="flex flex-col md:flex-row items-start md:items-center justify-between gap-2 md:gap-4 px-3 md:px-lg py-2 md:py-3 border-b border-outline-variant/30">
          <div className="text-base md:text-[18px] font-bold text-on-surface min-w-0 truncate">{title}</div>
          {actions && <div className="flex items-center gap-sm shrink-0 w-full md:w-auto">{actions}</div>}
        </div>
      )}
    </header>
  );
}
