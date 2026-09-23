/** @type {import('next').NextConfig} */

const nextConfig = {
  reactStrictMode: true,

  allowedDevOrigins: [
    '172.20.10.2',
  ],

  images: {
    remotePatterns: [
      {
        protocol: 'http',
        hostname: '172.20.10.2',
      },
    ],
  },
};

export default nextConfig;