import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // PWA manifest + service worker will be added via next-pwa when ready
  experimental: {
    serverActions: { bodySizeLimit: "2mb" },
  },
  images: {
    remotePatterns: [
      {
        protocol: "https",
        hostname: "*.supabase.co",
      },
    ],
  },
};

export default nextConfig;
