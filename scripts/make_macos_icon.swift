#!/usr/bin/swift
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

guard CommandLine.arguments.count == 3 else {
    fputs("usage: make_macos_icon.swift input.png output.png\n", stderr)
    exit(2)
}

let input = URL(fileURLWithPath: CommandLine.arguments[1])
let output = URL(fileURLWithPath: CommandLine.arguments[2])
guard let source = CGImageSourceCreateWithURL(input as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fputs("unable to read input image\n", stderr)
    exit(1)
}

let width = image.width
let height = image.height
let colorSpace = CGColorSpaceCreateDeviceRGB()
guard let context = CGContext(
    data: nil,
    width: width,
    height: height,
    bitsPerComponent: 8,
    bytesPerRow: 0,
    space: colorSpace,
    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
) else {
    fputs("unable to create image context\n", stderr)
    exit(1)
}

context.clear(CGRect(x: 0, y: 0, width: width, height: height))
let shortest = CGFloat(min(width, height))
// The generated artwork already contains a finished rounded tile, surrounded by
// an opaque black export canvas. Crop that canvas away before placing the tile
// on a transparent macOS icon canvas. A previous implementation only masked the
// full export, which left a visible black square around the actual icon.
let sourceSide = shortest * 0.888
let sourceRect = CGRect(
    x: (CGFloat(width) - sourceSide) / 2,
    y: (CGFloat(height) - sourceSide) / 2,
    width: sourceSide,
    height: sourceSide
)
guard let croppedImage = image.cropping(to: sourceRect) else {
    fputs("unable to crop source artwork\n", stderr)
    exit(1)
}

// Match the visual footprint of current macOS app icons while keeping the
// rounded-square exterior fully transparent.
let inset = shortest * 0.09
let radius = shortest * 0.17
let tile = CGRect(x: inset, y: inset, width: CGFloat(width) - inset * 2, height: CGFloat(height) - inset * 2)
context.addPath(CGPath(roundedRect: tile, cornerWidth: radius, cornerHeight: radius, transform: nil))
context.clip()
context.interpolationQuality = .high
context.draw(croppedImage, in: tile)

guard let result = context.makeImage(),
      let destination = CGImageDestinationCreateWithURL(output as CFURL, UTType.png.identifier as CFString, 1, nil) else {
    fputs("unable to create output image\n", stderr)
    exit(1)
}
CGImageDestinationAddImage(destination, result, nil)
guard CGImageDestinationFinalize(destination) else {
    fputs("unable to save output image\n", stderr)
    exit(1)
}
