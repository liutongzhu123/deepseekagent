(function (root) {
  function filterReviewQueueItems(items, status, hasPendingReview) {
    if (!status) return items;
    return items.filter((item) => item.status === status || hasPendingReview(item));
  }

  function isDefinitiveClientRejection(statusCode) {
    return Number.isInteger(statusCode) && statusCode >= 400 && statusCode < 500
      && ![408, 425, 429].includes(statusCode);
  }

  function filterWithPendingOverride(items, status, matchesOtherFilters, hasPendingReview) {
    return filterReviewQueueItems(items, status, hasPendingReview)
      .filter((item) => hasPendingReview(item) || matchesOtherFilters(item));
  }

  filterReviewQueueItems.isDefinitiveClientRejection = isDefinitiveClientRejection;
  filterReviewQueueItems.filterWithPendingOverride = filterWithPendingOverride;
  if (typeof module !== 'undefined' && module.exports) module.exports = filterReviewQueueItems;
  else {
    root.filterReviewQueueItems = filterReviewQueueItems;
    root.isDefinitiveReviewActionRejection = isDefinitiveClientRejection;
  }
})(typeof window === 'undefined' ? globalThis : window);
