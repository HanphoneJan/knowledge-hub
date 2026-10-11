import { describe, expect, it } from "vitest";

import {
  PHOTO_PAGE_SIZE,
  parsePhotoPage,
  photoPages,
  shownPhotoPage,
} from "./photos";

// ⚠️ 原来这里有一段「🔌 接线：PhotosPage 必须真的用这份纯函数」的**源码文本守卫**
//   （读 `../pages/PhotosPage.tsx`，断言它 `from "../lib/photos"` 且调 `shownPhotoPage(`）。
//   D188 全量重写 UI 时页面被删除 ⇒ 该守卫无的放矢，已移除。
//   🔑 新页面写好之后，**应该把它加回来**（指向新照片页文件），否则这份纯函数会
//   "带着自己的单测全绿"而组件其实没接线。行为层（深链 ?page=2 夹回）重写时也要覆盖。

describe("PHOTO_PAGE_SIZE（页码常量只有一份）", () => {
  it("= 24（与翻页器算法同源）", () => {
    expect(PHOTO_PAGE_SIZE).toBe(24);
  });
});

describe("parsePhotoPage（URL 里的页码文本 → 合法页码）", () => {
  it("正常值原样返回", () => {
    expect(parsePhotoPage("1")).toBe(1);
    expect(parsePhotoPage("3")).toBe(3);
  });

  it("缺省 / 空 → 1", () => {
    expect(parsePhotoPage(null)).toBe(1);
    expect(parsePhotoPage(undefined)).toBe(1);
    expect(parsePhotoPage("")).toBe(1);
  });

  // 🔑 非法值必须落到 1，而不是 NaN：NaN 会变成 `page=NaN` 发给服务端（400），
  //   而报错完全指不到"是前端的 URL 解析坏了"。
  it("非数字 → 1（不是 NaN）", () => {
    expect(parsePhotoPage("abc")).toBe(1);
    expect(parsePhotoPage("2x")).toBe(2); // parseInt 的正常语义：取前缀
    expect(Number.isNaN(parsePhotoPage("abc"))).toBe(false);
  });

  it("0 与负数 → 1（翻页器不能显示「第 0 / 3 页」）", () => {
    expect(parsePhotoPage("0")).toBe(1);
    expect(parsePhotoPage("-3")).toBe(1);
  });
});

describe("photoPages（总页数至少为 1）", () => {
  it("按 PAGE_SIZE 向上取整", () => {
    expect(photoPages(0)).toBe(1);
    expect(photoPages(1)).toBe(1);
    expect(photoPages(PHOTO_PAGE_SIZE)).toBe(1);
    expect(photoPages(PHOTO_PAGE_SIZE + 1)).toBe(2);
    expect(photoPages(PHOTO_PAGE_SIZE * 2)).toBe(2);
  });

  it("🔑 0 张照片也是 1 页（不是 0 页：会显示成「第 1 / 0 页」）", () => {
    expect(photoPages(0)).toBe(1);
  });
});

describe("shownPhotoPage（D150：夹回，但首次加载不夹）", () => {
  it("页码在范围内 → 原样", () => {
    expect(shownPhotoPage(2, 100, true)).toBe(2);
    expect(shownPhotoPage(1, 100, true)).toBe(1);
  });

  // 🔴 这条是 D150 的核心：深链 / 书签的页码超出范围时要夹回，
  //   否则会请求一个不存在的页 ⇒ 0 行 ⇒ 空白页，而翻页器又不渲染
  //   （pages=1）⇒ 用户卡死，没有回到第 1 页的路。
  it("🔑 超出范围 → 夹到最后一页（不是空白页）", () => {
    expect(shownPhotoPage(9, 13, true)).toBe(1); // 13 张 → 只有 1 页
    // ⚠️ 100/24 = 4.17 ⇒ **5** 页（我第一版在这里写 3，被自己的测试抓住）
    expect(photoPages(100)).toBe(5);
    expect(shownPhotoPage(5, 100, true)).toBe(5); // 第 5 页合法，不该被夹
    expect(shownPhotoPage(9, 100, true)).toBe(5); // 第 9 页越界 ⇒ 夹到 5
  });

  // 🔑 这条是最容易被"简化"掉的分支：首次加载时 total 还是 0（⇒ pages=1），
  //   一夹就把合法的深链 ?page=3 变成第 1 页，而用户以为自己在看第 3 页。
  it("🔑 首次加载**不夹**（那时还不知道总数）", () => {
    expect(shownPhotoPage(3, 0, false)).toBe(3);
    expect(shownPhotoPage(3, 13, false)).toBe(3);
  });

  it("夹取与首次加载两条规则合起来：深链 ?page=2 到 13 张的库 → 第 1 页", () => {
    // 第一次渲染：total=0、不夹 ⇒ 用第 2 页去请求
    expect(shownPhotoPage(2, 0, false)).toBe(2);
    // 拿到 total=13 之后：夹回第 1 页 ⇒ 照片照常显示
    expect(shownPhotoPage(2, 13, true)).toBe(1);
  });

  it("非法页码也不会越界（parsePhotoPage + 夹取双保险）", () => {
    expect(shownPhotoPage(0, 100, true)).toBe(1);
    expect(shownPhotoPage(-5, 100, true)).toBe(1);
  });
});
