"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, User } from "@/lib/api";
import Sidebar from "./Sidebar";
import TopBar from "./TopBar";
import KeyboardShortcuts from "./KeyboardShortcuts";

export default function AppShell({
  title,
  actions,
  children,
}: {
  title: React.ReactNode;
  actions?: React.ReactNode;
  children: React.ReactNode;
}) {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "anon">("loading");
  const [collapsed, setCollapsed] = useState<boolean>(false);
  // Trên mobile/tablet: false = thanh 70px (mặc định), true = phình ra 260px.
  const [mobileOpen, setMobileOpen] = useState(false);

  useEffect(() => {
    const saved = localStorage.getItem("sidebar_collapsed");
    if (saved === "true") {
      setCollapsed(true);
    }
  }, []);

  const toggleSidebar = () => {
    setCollapsed((prev) => {
      const next = !prev;
      localStorage.setItem("sidebar_collapsed", String(next));
      return next;
    });
  };

  useEffect(() => {
    (async () => {
      try {
        const u = await api.me();
        setUser(u);
        setState("ready");
      } catch {
        setState("anon");
        router.replace("/login");
      }
    })();
  }, [router]);

  if (state === "loading") {
    return (
      <div className="min-h-screen grid place-items-center text-on-surface-variant">
        <div className="flex items-center gap-sm">
          <span className="material-symbols-outlined animate-spin">progress_activity</span>
          Đang tải…
        </div>
      </div>
    );
  }
  if (state === "anon") return null;

  return (
    <div>
      <KeyboardShortcuts />

      {/* Sidebar: tự quản lý responsive bên trong (70px mặc định trên mobile,
          toggle qua nút trong header của chính nó; 70px/260px trên desktop qua collapsed) */}
      <Sidebar
        user={user}
        collapsed={collapsed}
        onToggle={toggleSidebar}
        mobileOpen={mobileOpen}
        onMobileToggle={() => setMobileOpen((v) => !v)}
      />

      {/* Nội dung chính:
          - Mobile nhỏ (<640px): sidebar ẩn hoàn toàn ngoài màn hình → không margin.
          - Tablet (640–1023px): sidebar luôn hiện thanh 70px → margin 70px.
          - Desktop (≥1024px): margin khớp theo collapsed (70px hoặc 260px).
          Khi mở rộng (mobileOpen/collapsed=false), sidebar đè overlay lên
          nội dung nên margin KHÔNG đổi theo mobileOpen, chỉ theo collapsed. */}
      <main
        className={`flex flex-col min-h-screen bg-white transition-all duration-300 ml-0 sm:ml-[70px] ${
          collapsed ? "" : "lg:ml-[260px]"
        }`}
      >
        <TopBar title={title} user={user} actions={actions} />
        <div className="flex-grow">{children}</div>
      </main>
    </div>
  );
}