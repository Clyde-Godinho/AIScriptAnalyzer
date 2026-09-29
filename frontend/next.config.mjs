/** @type {import('next').NextConfig} */
const BACKEND =
  process.env.BACKEND_ORIGIN ?? process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    // Proxy /api/* and /health to the FastAPI backend so the browser never
    // deals with CORS.
    return [
      { source: "/api/:path*", destination: `${BACKEND}/api/:path*` },
      { source: "/health", destination: `${BACKEND}/health` },
    ];
  },
};

export default nextConfig;
