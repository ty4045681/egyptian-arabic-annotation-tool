/**
 * Ant Design theme configs generated from the shared token source.
 * No per-page color tables: both configs map the same semantic tokens.
 */
import { theme as antdTheme, type ThemeConfig } from "antd";
import {
  controlHeight,
  fontFamily,
  fontSizeUI,
  adminPalette,
  adminDarkPalette,
  annotatorPalette,
  radiusControl,
} from "./tokens";

export const adminTheme: ThemeConfig = {
  algorithm: antdTheme.defaultAlgorithm,
  token: {
    colorPrimary: adminPalette.primary,
    colorInfo: adminPalette.primary,
    colorLink: adminPalette.primary,
    colorBgLayout: adminPalette.background,
    colorBgContainer: adminPalette.surface,
    colorBorder: adminPalette.border,
    colorText: adminPalette.text,
    colorTextSecondary: adminPalette.textSecondary,
    fontSize: fontSizeUI,
    fontFamily,
    borderRadius: 8,
    borderRadiusSM: 8,
    borderRadiusLG: 12,
    controlHeight: 34,
    controlHeightSM: 28,
  },
  components: {
    Table: {
      headerBg: adminPalette.surfaceAlt,
    },
  },
};

export const adminDarkTheme: ThemeConfig = {
  ...adminTheme,
  algorithm: antdTheme.darkAlgorithm,
  token: {
    ...adminTheme.token,
    colorPrimary: adminDarkPalette.primary,
    colorInfo: adminDarkPalette.primary,
    colorLink: adminDarkPalette.primary,
    colorBgLayout: adminDarkPalette.background,
    colorBgContainer: adminDarkPalette.surface,
    colorBgElevated: adminDarkPalette.surface,
    colorText: adminDarkPalette.text,
    colorTextSecondary: adminDarkPalette.textSecondary,
    colorBorder: adminDarkPalette.border,
  },
  components: { Table: { headerBg: adminDarkPalette.surfaceAlt } },
};

export const annotatorTheme: ThemeConfig = {
  algorithm: antdTheme.darkAlgorithm,
  token: {
    colorPrimary: annotatorPalette.primary,
    colorBgLayout: annotatorPalette.background,
    colorBgContainer: annotatorPalette.surface,
    colorBorder: annotatorPalette.border,
    colorText: annotatorPalette.text,
    colorTextSecondary: annotatorPalette.textSecondary,
    fontSize: fontSizeUI,
    fontFamily,
    borderRadius: radiusControl,
    controlHeight,
  },
};
