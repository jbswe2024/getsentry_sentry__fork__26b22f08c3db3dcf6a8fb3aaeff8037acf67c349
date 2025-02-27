import {useMemo} from 'react';

import {
  decodeBoolean,
  decodeInteger,
  type decodeList,
  decodeScalar,
  type decodeSorts,
} from 'sentry/utils/queryString';
import {useLocation} from 'sentry/utils/useLocation';

type Scalar = string | boolean | number | undefined;
export type Decoder =
  | typeof decodeInteger
  | typeof decodeList
  | typeof decodeScalar
  | typeof decodeSorts
  | typeof decodeBoolean;

/**
 * Select and memoize query params from location.
 * This returns a new object only when one of the specified query fields is
 * updated. The object will remain stable otherwise, avoiding re-renders.
 *
 * You shouldn't need to manually set the `InferredRequestShape` or `InferredResponseShape`
 * generics, instead type the left side of the statement.
 *
 * For example:
 * ```
 * type QueryFields = {statsPeriod: string};
 * const query: QueryFields = useLocationQuery({
 *   fields: {statsPeriod: decodeScalar}
 * });
 * ```
 */
export default function useLocationQuery<
  InferredRequestShape extends Record<string, Scalar | Scalar[] | Decoder>,
  InferredResponseShape extends {
    readonly [Property in keyof InferredRequestShape]: InferredRequestShape[Property] extends Decoder
      ? ReturnType<InferredRequestShape[Property]>
      : InferredRequestShape[Property];
  },
>({fields}: {fields: InferredRequestShape}): InferredResponseShape {
  const location = useLocation();

  const locationFields = {};
  const forwardedFields = {};
  Object.entries(fields).forEach(([field, decoderOrValue]) => {
    if (typeof decoderOrValue === 'function') {
      if (decoderOrValue === decodeScalar) {
        locationFields[field] = decoderOrValue(location.query[field], '');
      } else if (decoderOrValue === decodeBoolean) {
        locationFields[field] = decoderOrValue(location.query[field], false);
      } else if (decoderOrValue === decodeInteger) {
        locationFields[field] = decoderOrValue(location.query[field], 0);
      } else {
        locationFields[field] = decoderOrValue(location.query[field]);
      }
    } else {
      forwardedFields[field] = decoderOrValue;
    }
  }, {});

  const stringyForwardedFields = JSON.stringify(forwardedFields);
  const stringyLocationFields = JSON.stringify(locationFields);

  return useMemo(
    () => ({
      ...(forwardedFields as any),
      ...(locationFields as any),
    }),
    [stringyForwardedFields, stringyLocationFields] // eslint-disable-line
  );
}
